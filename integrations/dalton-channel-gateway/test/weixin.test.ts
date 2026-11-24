import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { ChannelDeliveryError } from "../src/channels/adapter.js";
import {
  aesEcbPaddedSize,
  buildWeixinScopedMessageId,
  CONTEXT_TOKEN_STALE_ERRCODES,
  decryptAes128Ecb,
  downloadWeixinCdnAttachment,
  encodeOutboundAesKeyBase64,
  encryptAes128Ecb,
  filterWeixinMarkdown,
  getWeixinUpdates,
  isSessionExpiredCode,
  normalizeAndValidateCdnBaseUrl,
  normalizeAndValidateIlinkBaseUrl,
  parseInboundAesKey,
  parseRetryAfterMs,
  parseWeixinApiJson,
  parseWeixinInboundMessage,
  pollWeixinQrStatus,
  resolvePersistedAttachmentMeta,
  resolveValidatedCdnDownloadUrl,
  resolveValidatedRedirectBaseUrl,
  sendWeixinMediaAttachment,
  sendWeixinTextMessage,
  shapePublicQrWaitResponse,
  WEIXIN_MIXED_UNSUPPORTED_NOTE,
  WEIXIN_POLL_POOL_TIMEOUT_MS,
  WeixinApiError,
  WeixinChannelAdapter,
  WeixinHttpPoolManager,
  WeixinQrLoginError,
  WeixinQrLoginManager,
} from "../src/channels/weixin/index.js";
import { parseSlashCommand } from "../src/commands.js";
import {
  DEFAULT_DEFERRED_DELIVERY_POLL_INTERVAL_MS,
  DeferredDeliveryPoller,
} from "../src/deferred-delivery-poller.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import {
  DEFAULT_GATEWAY_LEASE_ACQUIRE_RETRY_MS,
  GatewayLeaseManager,
  LeaseSafetyMarginPauseError,
  StaleGatewayEpochError,
} from "../src/gateway-lease.js";
import {
  createGatewayApp,
  sanitizePublicWeixinQrResponse,
} from "../src/server.js";
import {
  createSubsystemLogger,
  formatSafeSessionKeyPrefix,
  redactCredentialString,
} from "../src/shared/logger.js";
import {
  MAX_CIPHERTEXT_ATTACHMENT_BYTES,
  SafeFetchError,
  safeFetchResponse,
} from "../src/shared/safe-fetch.js";
import {
  BackendHttpError,
  HttpBackendClient,
  type I1RegisterAndResolveRequest,
  type I1RegisterAndResolveResponse,
  type RecoverInboundEventRow,
  type WeixinAccountSnapshot,
  type WeixinLoginSessionGetResponse,
  type WeixinPreflightAuthorizedData,
} from "../src/backend-client.js";
import { loadConfig } from "../src/config.js";
import {
  type ChannelPlatform,
  type InboundChannelEvent,
  MAX_ATTACHMENT_BYTES,
} from "../src/types.js";
import {
  createMockBackend,
  createTestHarness,
  fetchGatewayServer,
  HOME_WS_ID,
  TEST_SERVICE_TOKEN,
  USER_ID,
} from "./helpers.js";

function defer<T = void>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function emptyInbound(
  req: I1RegisterAndResolveRequest,
): I1RegisterAndResolveResponse {
  return {
    existing_status: null,
    linked: false,
    allowed: false,
    deny_reason: null,
    dalton_user_id: null,
    request_key: req.request_key,
    retry_request_key: null,
    resubmitted_request_key: null,
    steered_into_run_id: null,
    thread_id: null,
    run_id: null,
    turn_index: null,
    workspace_id: null,
    external_thread_id: null,
    generation: 1,
    fallback_pending: false,
    sent_to_current_count: 0,
    uploaded_attachments: [],
    raw_payload: req.raw_payload ?? {},
    pending_interrupt: null,
  };
}

function weixinDm(
  messageId: string,
  text: string,
  extra: Partial<InboundChannelEvent> = {},
): InboundChannelEvent {
  return {
    platform: "weixin",
    platformUserId: "wx_alice",
    chatType: "dm",
    chatAddress: "weixin:@me",
    chatId: "wx_alice",
    threadScopeKey: "dm:wx_alice",
    messageId,
    text,
    weixinAccountId: "wx_acc_1",
    weixinBotId: "bot_live_1",
    weixinBotGeneration: 5,
    weixinContextToken: "ctx_live_1",
    ...extra,
  };
}

function makeAccountSnapshot(
  overrides: Partial<WeixinAccountSnapshot> = {},
): WeixinAccountSnapshot {
  return {
    account_id: "wx_acc_1",
    user_id: USER_ID,
    ilink_bot_id: "bot_live_1",
    ilink_user_id: "wx_owner",
    bot_token: "bot_tok_live",
    base_url: "https://ilinkai.weixin.qq.com",
    cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
    get_updates_buf: "buf_0",
    status: "active",
    bot_generation: 5,
    updated_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

function makePreflightAllow(
  overrides: Partial<WeixinPreflightAuthorizedData> = {},
): WeixinPreflightAuthorizedData {
  return {
    authorized: true,
    decision: "allow",
    retryable: false,
    chunk_index: 0,
    account_id: "wx_acc_1",
    ilink_bot_id: "bot_live_1",
    bot_generation: 5,
    bot_token: "bot_tok_live",
    base_url: "https://ilinkai.weixin.qq.com",
    cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
    recipient_id: "wx_alice",
    weixin_context_token: "ctx_live_1",
    weixin_context_token_version: 1,
    ...overrides,
  };
}

function headerValue(init: RequestInit | undefined, name: string): string {
  const headers = init?.headers;
  if (!headers) return "";
  if (headers instanceof Headers) return headers.get(name) ?? "";
  if (Array.isArray(headers)) {
    const found = headers.find(([key]) => key.toLowerCase() === name.toLowerCase());
    return found ? String(found[1]) : "";
  }
  const record = headers as Record<string, string>;
  const match = Object.entries(record).find(
    ([key]) => key.toLowerCase() === name.toLowerCase(),
  );
  return match ? String(match[1]) : "";
}

function makeJsonResponse(
  body: unknown,
  status = 200,
  headers?: Record<string, string>,
): Response {
  const raw = typeof body === "string" ? body : JSON.stringify(body);
  return new Response(raw, {
    status,
    headers: {
      "content-type": "application/json",
      ...(headers ?? {}),
    },
  });
}

function waitBounded<T>(promise: Promise<T>, label: string, ms = 2000): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new Error(`${label} timed out after ${ms}ms`));
    }, ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

function pausedCdnFetch(): {
  fetchImpl: typeof fetch;
  uploadEntered: ReturnType<typeof defer<void>>;
  releaseUpload: ReturnType<typeof defer<void>>;
  uploadAuthorizations: string[];
  sendMessages: Array<{ body: string; authorization: string }>;
  cdnCalls: number;
} {
  const uploadEntered = defer<void>();
  const releaseUpload = defer<void>();
  const uploadAuthorizations: string[] = [];
  const sendMessages: Array<{ body: string; authorization: string }> = [];
  let cdnCalls = 0;
  const fetchImpl: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.includes("getuploadurl")) {
      uploadAuthorizations.push(headerValue(init, "authorization"));
      return makeJsonResponse({ ret: 0, upload_param: "up_param_file" });
    }
    if (url.includes("novac2c.cdn.weixin.qq.com")) {
      cdnCalls += 1;
      uploadEntered.resolve();
      await waitBounded(releaseUpload.promise, "cdn upload pause", 1500);
      return new Response("", {
        status: 200,
        headers: { "x-encrypted-param": "cdn_enc_file" },
      });
    }
    if (url.includes("sendmessage")) {
      sendMessages.push({
        body: String(init?.body ?? ""),
        authorization: headerValue(init, "authorization"),
      });
      return makeJsonResponse({ ret: 0, message_id: "wx_file_sent" });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };
  return {
    fetchImpl,
    uploadEntered,
    releaseUpload,
    uploadAuthorizations,
    sendMessages,
    get cdnCalls() {
      return cdnCalls;
    },
  };
}

describe("Weixin iLink Adapter — Protocol, Crypto, Inbound, QR Concurrency & Lifecycle", () => {
  it("preserves lossless uint64 message_id, msg_id, and svr_id strings in parseWeixinApiJson", () => {
    const raw =
      '{"ret":0,"msgs":[{"message_id":18446744073709551615,"ref_msg":{"svr_id":9007199254740993123,"message_item":{"msg_id":18446744073709551614}}}]}';
    const parsed = parseWeixinApiJson<{
      ret: number;
      msgs: Array<{
        message_id: string;
        ref_msg: { svr_id: string; message_item: { msg_id: string } };
      }>;
    }>(raw);

    expect(parsed.msgs[0]?.message_id).toBe("18446744073709551615");
    expect(parsed.msgs[0]?.ref_msg.svr_id).toBe("9007199254740993123");
    expect(parsed.msgs[0]?.ref_msg.message_item.msg_id).toBe(
      "18446744073709551614",
    );
  });

  it("encrypts and decrypts AES-128-ECB with PKCS#7 padding and supports both inbound key encodings + plaintext fallback", async () => {
    const keyHex = "00112233445566778899aabbccddeeff";
    const key = Buffer.from(keyHex, "hex");
    const pngSample = Buffer.concat([
      Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
      Buffer.from("dalton-weixin-image-payload", "utf8"),
    ]);
    const ciphertext = encryptAes128Ecb(pngSample, key);
    expect(ciphertext.byteLength).toBe(aesEcbPaddedSize(pngSample.byteLength));
    expect(decryptAes128Ecb(ciphertext, key).equals(pngSample)).toBe(true);

    const k1 = parseInboundAesKey({ aesKeyHex: keyHex });
    expect(k1.equals(key)).toBe(true);
    const k2 = parseInboundAesKey({ aesKeyBase64: key.toString("base64") });
    expect(k2.equals(key)).toBe(true);
    const k3 = parseInboundAesKey({
      aesKeyBase64: Buffer.from(keyHex, "utf8").toString("base64"),
    });
    expect(k3.equals(key)).toBe(true);
    expect(encodeOutboundAesKeyBase64(key)).toBe(
      Buffer.from(keyHex, "utf8").toString("base64"),
    );

    const plainFetch: typeof fetch = vi.fn(async (_input, init) => {
      const headers = new Headers(init?.headers);
      expect(headers.get("authorization")).toBeNull();
      expect(headers.get("x-service-token")).toBeNull();
      return new Response(new Uint8Array(pngSample), { status: 200 });
    });
    const plainDownloaded = await downloadWeixinCdnAttachment(
      {
        id: "att_plain",
        kind: "image",
        encryptQueryParam: "enc_q_1",
        filename: "chart",
        mimeType: "image/jpeg",
      },
      {
        cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
        allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
        fetchImpl: plainFetch,
        checkResolvedIps: false,
      },
    );
    expect(plainDownloaded.buffer.equals(pngSample)).toBe(true);
    expect(plainDownloaded.mimeType).toBe("image/png");
    expect(plainDownloaded.filename).toBe("chart.png");

    await expect(
      downloadWeixinCdnAttachment(
        {
          id: "att_bad_key",
          kind: "image",
          encryptQueryParam: "enc_q_2",
          aesKeyHex: "ffeeddccbbaa99887766554433221100",
          filename: "chart.png",
          mimeType: "image/png",
        },
        {
          cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
          fetchImpl: async () =>
            new Response(new Uint8Array(ciphertext), { status: 200 }),
          checkResolvedIps: false,
        },
      ),
    ).rejects.toThrow();
  });

  it("accepts an encrypted file whose plaintext is exactly 20 MiB (ciphertext 20 MiB + 16 bytes) and rejects declared size mismatch and corrupted padding", async () => {
    const keyHex = "00112233445566778899aabbccddeeff";
    const key = Buffer.from(keyHex, "hex");
    const exact20MiB = Buffer.alloc(MAX_ATTACHMENT_BYTES, 0x41);
    const cipher20MiB = encryptAes128Ecb(exact20MiB, key);
    expect(cipher20MiB.length).toBe(MAX_CIPHERTEXT_ATTACHMENT_BYTES);

    const downloaded = await downloadWeixinCdnAttachment(
      {
        id: "att_20m",
        kind: "file",
        encryptQueryParam: "enc_20m",
        aesKeyHex: keyHex,
        filename: "report.pdf",
        sizeBytes: MAX_ATTACHMENT_BYTES,
        mimeType: "application/pdf",
      },
      {
        cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
        allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
        fetchImpl: async () =>
          new Response(new Uint8Array(cipher20MiB), { status: 200 }),
        checkResolvedIps: false,
      },
    );
    expect(downloaded.buffer.length).toBe(MAX_ATTACHMENT_BYTES);

    const validShortCipher = encryptAes128Ecb(Buffer.alloc(32, 0x42), key);
    await expect(
      downloadWeixinCdnAttachment(
        {
          id: "att_mismatch",
          kind: "file",
          encryptQueryParam: "enc_mismatch",
          aesKeyHex: keyHex,
          filename: "report.pdf",
          sizeBytes: 1024,
          mimeType: "application/pdf",
        },
        {
          cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
          fetchImpl: async () =>
            new Response(new Uint8Array(validShortCipher), { status: 200 }),
          checkResolvedIps: false,
        },
      ),
    ).rejects.toThrow(/does not match declared file len/i);

    await expect(
      downloadWeixinCdnAttachment(
        {
          id: "att_bad_padding",
          kind: "file",
          encryptQueryParam: "enc_bad_padding",
          aesKeyHex: keyHex,
          filename: "report.pdf",
          mimeType: "application/pdf",
        },
        {
          cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
          fetchImpl: async () =>
            new Response(new Uint8Array(cipher20MiB.subarray(0, 64)), {
              status: 200,
            }),
          checkResolvedIps: false,
        },
      ),
    ).rejects.toThrow(/bad decrypt|padding/i);
  });

  it("enforces HTTPS, allowed hosts, no userinfo, and private-IP SSRF checks on API, CDN, and redirect URLs", async () => {
    const allowedApi = ["ilinkai.weixin.qq.com", "weixin.qq.com"];
    const allowedCdn = ["novac2c.cdn.weixin.qq.com", "weixin.qq.com"];

    expect(
      normalizeAndValidateIlinkBaseUrl(
        "https://ilinkai.weixin.qq.com",
        allowedApi,
      ),
    ).toBe("https://ilinkai.weixin.qq.com");

    expect(
      normalizeAndValidateCdnBaseUrl(
        "https://novac2c.cdn.weixin.qq.com/c2c",
        allowedCdn,
      ),
    ).toBe("https://novac2c.cdn.weixin.qq.com/c2c");

    expect(() =>
      normalizeAndValidateIlinkBaseUrl(
        "http://ilinkai.weixin.qq.com",
        allowedApi,
      ),
    ).toThrow(/Unsupported attachment URL protocol: http:/i);

    expect(() =>
      resolveValidatedCdnDownloadUrl({
        fullUrl: "https://user:secret@novac2c.cdn.weixin.qq.com/c2c/download",
        allowedCdnDomains: allowedCdn,
      }),
    ).toThrow(/Embedded credentials/i);

    try {
      resolveValidatedCdnDownloadUrl({
        fullUrl: "https://evil.example.com/c2c/download",
        allowedCdnDomains: allowedCdn,
      });
      expect.unreachable("Should have rejected disallowed CDN host");
    } catch (err) {
      expect(err).toBeInstanceOf(SafeFetchError);
      if (err instanceof SafeFetchError) {
        expect(err.code).toBe("disallowed_domain");
        expect(err.message).toMatch(/not in the allowed domain list/i);
      }
    }

    await expect(
      resolveValidatedRedirectBaseUrl({
        redirectHost: "https://127.0.0.1/ilink/bot/get_qrcode_status",
        allowedDomains: ["127.0.0.1", ...allowedApi],
        checkResolvedIps: true,
      }),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "blocked_host",
      message: expect.stringMatching(/host is blocked:\s*127\.0\.0\.1/i),
    });
  });

  it("parses mixed text + voice transcription in order, isolates inline quotes from slash commands, handles voice without transcription, and reconstructs attachments prioritizing decrypted_weixin_attachments", () => {
    const msg = {
      message_id: "18446744073709551610",
      seq: 42,
      from_user_id: "wx_user_alice",
      to_user_id: "ilink_bot_1",
      context_token: "ctx_tok_secret_42",
      item_list: [
        {
          type: 1,
          text_item: { text: "First segment" },
          ref_msg: {
            title: "Previous turn",
            message_item: {
              type: 1,
              text_item: { text: "/new should not trigger command" },
            },
          },
        },
        {
          type: 3,
          voice_item: { text: "Second segment from voice transcription" },
        },
        {
          type: 2,
          image_item: {
            aeskey: "00112233445566778899aabbccddeeff",
            hd_size: 4096,
            media: {
              encrypt_query_param: "enc_img_1",
            },
          },
        },
      ],
    };

    const event = parseWeixinInboundMessage(msg, {
      accountId: "wx_acc_1",
      ilinkBotId: "ilink_bot_1",
      botGeneration: 3,
    });

    expect(event.chatType).toBe("dm");
    expect(event.weixinAccountId).toBe("wx_acc_1");
    expect(event.messageId).toBe(
      buildWeixinScopedMessageId("ilink_bot_1", msg),
    );
    expect(buildWeixinScopedMessageId("ilink_bot_1", msg)).not.toBe(
      buildWeixinScopedMessageId("ilink_bot_2", msg),
    );

    expect(event.commandText).toBe(
      "First segment\nSecond segment from voice transcription",
    );
    expect(event.text).toContain(
      "> [引用] Previous turn | /new should not trigger command",
    );
    expect(event.weixinMediaMeta).toHaveLength(1);
    const descriptor = event.attachments?.[0];
    expect(descriptor).toBeDefined();
    if (!descriptor) {
      throw new Error("Expected attachment descriptor");
    }

    // decrypted_weixin_attachments takes precedence over raw_payload.weixin_attachments
    const prioritized = resolvePersistedAttachmentMeta(descriptor, {
      ...event,
      weixinMediaMeta: undefined,
      rawPayload: {
        decrypted_weixin_attachments: [
          {
            id: descriptor.id,
            kind: "image",
            filename: descriptor.filename,
            mimeType: "image/jpeg",
            encryptQueryParam: "decrypted_enc_img_1",
            aesKeyHex: "11223344556677889900aabbccddeeff",
          },
        ],
        weixin_attachments: [
          {
            id: descriptor.id,
            kind: "image",
            filename: descriptor.filename,
            mimeType: "image/jpeg",
            encryptQueryParam: "stale_raw_enc_img_1",
          },
        ],
      },
    });
    expect(prioritized?.encryptQueryParam).toBe("decrypted_enc_img_1");
    expect(prioritized?.aesKeyHex).toBe("11223344556677889900aabbccddeeff");

    // Non-empty group_id is rejected as group chat
    const groupMsg = parseWeixinInboundMessage(
      {
        message_id: "18446744073709551612",
        from_user_id: "wx_user_alice",
        group_id: "wx_group_999",
        item_list: [{ type: 1, text_item: { text: "/new" } }],
      },
      {
        accountId: "wx_acc_1",
        ilinkBotId: "ilink_bot_1",
        botGeneration: 3,
      },
    );
    expect(groupMsg.chatType).toBe("group");
    expect(groupMsg.weixinAccountId).toBe("wx_acc_1");
    expect(groupMsg.rawPayload?.rejected_reason).toBe(
      "weixin_group_unsupported",
    );
  });

  it("drives multi-account QR login state machine (starting -> save_qr -> redirect_host -> scaned -> verify_code -> confirm -> revoked/error codes)", async () => {
    const h = createTestHarness();
    try {
      const sessions = new Map<string, WeixinLoginSessionGetResponse>();
      sessions.set("sess_1", {
        session_key: "sess_1",
        user_id: "user_a",
        login_version: 1,
        expected_bot_generation: 0,
        status: "starting",
        expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      });

      vi.mocked(h.backend.getWeixinLoginSession).mockImplementation(async (key) => {
        const s = sessions.get(key);
        if (!s) {
          throw new BackendHttpError("not found", 404, { code: "not_found" });
        }
        return structuredClone(s);
      });

      let forceSupersededOnNextUpdate = false;
      vi.mocked(h.backend.patchWeixinLoginSession).mockImplementation(
        async (key, req) => {
          const s = sessions.get(key);
          if (!s) {
            throw new BackendHttpError("not found", 404, { code: "not_found" });
          }
          if (req.action === "save_qr") {
            s.qrcode = req.qrcode ?? null;
            s.qrcode_img_content = req.qrcode_img_content ?? null;
            s.redirect_base_url = req.redirect_base_url ?? null;
            s.status = "wait";
            s.login_version += 1;
            return {
              ok: true,
              revoked: false,
              session_key: key,
              status: "wait",
              login_version: s.login_version,
              expires_at: s.expires_at,
            };
          }
          if (req.action === "update") {
            if (forceSupersededOnNextUpdate) {
              s.status = "superseded";
              // Matches Batch A: HTTP 200 with ok:false, revoked:true
              return {
                ok: false,
                revoked: true,
                status: "superseded",
                code: "login_session_revoked",
                message: "Session superseded",
              };
            }
            s.status = req.status ?? s.status;
            s.redirect_base_url = req.redirect_base_url ?? s.redirect_base_url;
            s.login_version += 1;
            return {
              ok: true,
              revoked: false,
              session_key: key,
              status: s.status,
              login_version: s.login_version,
              redirect_base_url: s.redirect_base_url,
            };
          }
          if (req.action === "confirm") {
            if (req.ilink_bot_id === "bot_conflict") {
              throw new BackendHttpError("conflict", 409, {
                code: "weixin_account_already_bound",
                message: "Already bound to another user",
              });
            }
            s.status = "confirmed";
            s.connected = true;
            s.confirmed_result = {
              account_id: "wx_acc_confirmed",
              account_status: "pending_verification",
              ilink_bot_id: req.ilink_bot_id,
              ilink_user_id: req.ilink_user_id,
              bot_generation: 1,
              accounts_revision: 7,
            };
            return {
              ok: true,
              revoked: false,
              session_key: key,
              status: "confirmed",
              account_id: "wx_acc_confirmed",
              account_status: "pending_verification",
              ilink_bot_id: req.ilink_bot_id,
              ilink_user_id: req.ilink_user_id,
              connected: true,
              already_connected: false,
              bot_generation: 1,
              accounts_revision: 7,
            };
          }
          return { ok: true };
        },
      );

      let pollStep = 0;
      const pollHosts: string[] = [];
      const fakeFetch: typeof fetch = vi.fn(async (input) => {
        const parsed = new URL(String(input));
        if (parsed.pathname.includes("get_bot_qrcode")) {
          return makeJsonResponse({
            ret: 0,
            qrcode: "qr_tok_1",
            qrcode_img_content:
              "https://ilinkai.weixin.qq.com/qr/1?secret=must_strip",
          });
        }
        if (parsed.pathname.includes("get_qrcode_status")) {
          pollHosts.push(parsed.host);
          pollStep += 1;
          if (pollStep === 1) {
            return makeJsonResponse({
              ret: 0,
              status: "scaned_but_redirect",
              redirect_host: "sz.ilinkai.weixin.qq.com",
            });
          }
          if (pollStep === 2) {
            return makeJsonResponse({
              ret: 0,
              status: "need_verifycode",
            });
          }
          return makeJsonResponse({ ret: 0, status: "wait" });
        }
        if (parsed.pathname.includes("verify_qrcode_code")) {
          pollHosts.push(parsed.host);
          return makeJsonResponse({
            ret: 0,
            status: "confirmed",
            bot_token: "secret_bot_tok_never_leak",
            ilink_bot_id: "bot_user_a",
            ilink_user_id: "wx_user_a",
            baseurl: "https://sh.ilinkai.weixin.qq.com",
          });
        }
        throw new Error(`Unexpected URL: ${String(input)}`);
      });

      const confirmedAccounts: unknown[] = [];
      const qrManager = new WeixinQrLoginManager({
        backend: h.backend,
        baseUrl: "https://ilinkai.weixin.qq.com",
        allowedApiDomains: ["ilinkai.weixin.qq.com", "weixin.qq.com"],
        fetchImpl: fakeFetch,
        checkResolvedIps: false,
        onAccountConfirmed: async (acc) => {
          confirmedAccounts.push(acc);
        },
      });

      const started = await qrManager.startQrLogin("user_a", {
        sessionKey: "sess_1",
        loginVersion: 1,
        expectedBotGeneration: 0,
      });
      expect(started.status).toBe("wait");
      expect(String(started.qrcode_url)).not.toContain("must_strip");

      // Wrong user rejected with 403
      await expect(
        qrManager.waitQrLogin("user_b", "sess_1"),
      ).rejects.toMatchObject({ statusCode: 403 });

      // Step 1: scaned_but_redirect updates redirect_base_url
      const w1 = await qrManager.waitQrLogin("user_a", "sess_1");
      expect(w1.status).toBe("scaned");

      // Step 2: polls redirected host sz.ilinkai.weixin.qq.com -> verify_code
      const w2 = await qrManager.waitQrLogin("user_a", "sess_1");
      expect(w2.status).toBe("verify_code");
      expect(pollHosts).toEqual([
        "ilinkai.weixin.qq.com",
        "sz.ilinkai.weixin.qq.com",
      ]);

      // Step 3: submit verify_code -> confirm with confirmed baseurl sh.ilinkai.weixin.qq.com
      const confirmed = await qrManager.verifyQrCode("user_a", "sess_1", "123456");
      expect(confirmed.status).toBe("confirmed");
      expect(confirmed.account_id).toBe("wx_acc_confirmed");
      expect(confirmed.pending_verification).toBe(true);
      expect(confirmed).not.toHaveProperty("bot_token");
      expect(confirmedAccounts).toHaveLength(1);
      expect(confirmedAccounts[0]).toMatchObject({
        accountId: "wx_acc_confirmed",
        baseUrl: "https://sh.ilinkai.weixin.qq.com",
      });

      // Public response sanitizer preserves multi-account fields and strips secrets
      const sanitized = sanitizePublicWeixinQrResponse({
        ...confirmed,
        bot_token: "MUST_NOT_APPEAR",
        context_token: "MUST_NOT_APPEAR",
      });
      expect(sanitized.account_id).toBe("wx_acc_confirmed");
      expect(sanitized.pending_verification).toBe(true);
      expect(sanitized).not.toHaveProperty("bot_token");
      expect(sanitized).not.toHaveProperty("context_token");

      // Superseded HTTP 200 {ok:false, revoked:true} returns superseded status
      sessions.set("sess_2", {
        session_key: "sess_2",
        user_id: "user_a",
        login_version: 2,
        expected_bot_generation: 0,
        status: "wait",
        qrcode: "qr_tok_2",
        qrcode_img_content: "https://ilinkai.weixin.qq.com/qr/2",
        redirect_base_url: "https://ilinkai.weixin.qq.com",
        expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      });
      pollStep = 1;
      forceSupersededOnNextUpdate = true;
      const superseded = await qrManager.waitQrLogin("user_a", "sess_2");
      expect(superseded.status).toBe("superseded");
      expect(superseded.revoked).toBe(true);
      expect(superseded.code).toBe("login_session_revoked");
    } finally {
      await h.cleanup();
    }
  });

  it("advances per-account cursor only after all batch messages register, marks expired on -14, and stops cleanly", async () => {
    const h = createTestHarness();
    try {
      let pollCount = 0;
      const cursorSaves: string[] = [];
      let expiredSaved = false;
      let expiredErrorCode: unknown;

      vi.mocked(h.backend.reconcileWeixinAccounts).mockResolvedValue({
        changed: true,
        enabled: true,
        max_concurrent_accounts: 50,
        accounts_revision: 1,
        accounts: [makeAccountSnapshot({ get_updates_buf: "buf_0" })],
      });
      vi.mocked(h.backend.updateWeixinAccountCursor).mockImplementation(
        async (_accId, req) => {
          cursorSaves.push(req.get_updates_buf);
          return {
            ok: true,
            updated: true,
            bot_generation: req.expected_bot_generation,
            get_updates_buf: req.get_updates_buf,
            status: "active",
          };
        },
      );
      vi.mocked(h.backend.updateWeixinAccountStatus).mockImplementation(
        async (_accId, req) => {
          if (req.action === "mark_session_expired") {
            expiredSaved = true;
            expiredErrorCode = req.error_code;
          }
          return { ok: true, updated: true, accounts_revision: 2 };
        },
      );

      let failSecondMsgI1 = true;
      const fakeFetch: typeof fetch = vi.fn(async (input) => {
        const url = String(input);
        if (url.includes("getupdates")) {
          pollCount += 1;
          if (pollCount === 1 || pollCount === 2) {
            return makeJsonResponse({
              ret: 0,
              get_updates_buf: "buf_1",
              msgs: [
                {
                  message_id: "1001",
                  seq: 1,
                  from_user_id: "wx_u1",
                  context_token: "ctx_1",
                  item_list: [{ type: 1, text_item: { text: "msg 1" } }],
                },
                {
                  message_id: "1002",
                  seq: 2,
                  from_user_id: "wx_u1",
                  context_token: "ctx_2",
                  item_list: [{ type: 1, text_item: { text: "msg 2" } }],
                },
              ],
            });
          }
          return makeJsonResponse({
            ret: -14,
            errcode: -14,
            errmsg: "session expired secret",
          });
        }
        throw new Error(`Unexpected URL: ${url}`);
      });

      expect(isSessionExpiredCode(-14, 0)).toBe(true);
      expect(parseRetryAfterMs("7")).toBe(7000);

      const adapter = new WeixinChannelAdapter({
        backend: h.backend,
        baseUrl: "https://ilinkai.weixin.qq.com",
        cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
        retryDelayMs: 5,
        staggerDelayMs: 0,
        staggerJitterMs: 0,
        fetchImpl: fakeFetch,
        checkResolvedIps: false,
      });

      adapter.onInboundEvent(async (ev) => {
        expect(ev.weixinAccountId).toBe("wx_acc_1");
        if (ev.messageId.endsWith(":1002") && failSecondMsgI1) {
          failSecondMsgI1 = false;
          throw new Error("Simulated transient I-1 503 roster_lookup_error");
        }
      });

      await adapter.start();
      for (let i = 0; i < 40 && !expiredSaved; i += 1) {
        await new Promise((r) => setTimeout(r, 15));
      }

      expect(cursorSaves).toEqual(["buf_1"]);
      expect(expiredSaved).toBe(true);
      // Decision #11: backend expects error_code as string
      expect(expiredErrorCode).toBe("-14");
      expect(typeof expiredErrorCode).toBe("string");
      expect(adapter.getManagedAccounts()[0]?.status).toBe("expired");
      await adapter.stop();
    } finally {
      await h.cleanup();
    }
  });

  it("classifies outbound lost response as unknown, business error as failed, 429 with Retry-After as rate_limited, and reuses CDN upload parameters on retry", async () => {
    const neverFetch: typeof fetch = vi.fn(async () => {
      throw new Error("should not be called");
    });
    await expect(
      sendWeixinTextMessage(
        {
          baseUrl: "https://ilinkai.weixin.qq.com",
          cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          allowedApiDomains: ["ilinkai.weixin.qq.com"],
          botToken: "bot_tok",
          toUserId: "wx_u1",
          contextToken: "",
          fetchImpl: neverFetch,
          checkResolvedIps: false,
        },
        "Hello",
      ),
    ).rejects.toMatchObject({ outcome: "failed" });
    expect(neverFetch).not.toHaveBeenCalled();

    await expect(
      sendWeixinTextMessage(
        {
          baseUrl: "https://ilinkai.weixin.qq.com",
          cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          allowedApiDomains: ["ilinkai.weixin.qq.com"],
          botToken: "bot_tok",
          toUserId: "wx_u1",
          contextToken: "ctx_valid",
          fetchImpl: async () => {
            throw new Error("socket hang up after request sent");
          },
          checkResolvedIps: false,
        },
        "Hello",
      ),
    ).rejects.toMatchObject({ outcome: "unknown" });

    await expect(
      sendWeixinTextMessage(
        {
          baseUrl: "https://ilinkai.weixin.qq.com",
          cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          allowedApiDomains: ["ilinkai.weixin.qq.com"],
          botToken: "bot_tok",
          toUserId: "wx_u1",
          contextToken: "ctx_valid",
          fetchImpl: async () =>
            makeJsonResponse({ error: "too many requests" }, 429, {
              "retry-after": "7",
            }),
          checkResolvedIps: false,
        },
        "Hello",
      ),
    ).rejects.toMatchObject({ outcome: "rate_limited", retryAfterMs: 7000 });

    const getUploadBodies: Array<Record<string, unknown>> = [];
    const cdnPutBodies: Buffer[] = [];
    const sentMessages: Array<Record<string, unknown>> = [];
    let cdnAttempt = 0;

    const mediaFetch: typeof fetch = vi.fn(async (input, init) => {
      const url = String(input);
      if (url.includes("getuploadurl")) {
        getUploadBodies.push(JSON.parse(String(init?.body)));
        return makeJsonResponse({ ret: 0, upload_param: "up_param_1" });
      }
      if (url.includes("novac2c.cdn.weixin.qq.com")) {
        cdnAttempt += 1;
        if (init?.body instanceof Uint8Array) {
          cdnPutBodies.push(Buffer.from(init.body));
        }
        if (cdnAttempt === 1) {
          return new Response("gateway timeout", { status: 502 });
        }
        return new Response("", {
          status: 200,
          headers: { "x-encrypted-param": "cdn_enc_download_param" },
        });
      }
      if (url.includes("sendmessage")) {
        sentMessages.push(JSON.parse(String(init?.body)));
        return makeJsonResponse({ ret: 0 });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });

    const fileContent = Buffer.from("Dalton research memo content", "utf8");
    const sendRes = await sendWeixinMediaAttachment(
      {
        baseUrl: "https://ilinkai.weixin.qq.com",
        cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
        botToken: "bot_tok",
        toUserId: "wx_u1",
        contextToken: "ctx_valid",
        fetchImpl: mediaFetch,
        checkResolvedIps: false,
      },
      {
        filename: "memo.pdf",
        mimeType: "application/pdf",
        content: fileContent,
      },
    );

    expect(sendRes.status).toBe("sent");
    expect(getUploadBodies).toHaveLength(1);
    expect(cdnPutBodies).toHaveLength(2);
    expect(cdnPutBodies[0]?.equals(cdnPutBodies[1]!)).toBe(true);
    const serializedMsg = JSON.stringify(sentMessages[0]?.msg ?? {});
    expect(serializedMsg).toContain(`"len":"${fileContent.length}"`);
  });

  it("cancels a body that arrives after the headers when the timeout or caller aborts", async () => {
    const stalled = (flag: { cancelled: boolean }): ReadableStream<Uint8Array> =>
      new ReadableStream({
        start() {},
        cancel() {
          flag.cancelled = true;
        },
      });
    const timedOut = { cancelled: false };
    await expect(
      safeFetchResponse("https://ilinkai.weixin.qq.com/ilink/bot/getupdates", {
        timeoutMs: 30,
        checkResolvedIps: false,
        allowedDomains: ["ilinkai.weixin.qq.com"],
        fetchImpl: async () =>
          new Response(stalled(timedOut), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      }),
    ).rejects.toMatchObject({ name: "SafeFetchError", code: "timeout" });
    expect(timedOut.cancelled).toBe(true);
  });

  it("strips unsupported Markdown syntax via filterWeixinMarkdown and shapes public QR responses safely", () => {
    const md =
      "##### Heading 5\n\nHere is *中文斜体* and ![chart](https://example.com/a.png) plus **bold**.";
    const filtered = filterWeixinMarkdown(md);
    expect(filtered).toContain("Heading 5");
    expect(filtered).toContain("中文斜体");
    expect(filtered).not.toContain("*中文斜体*");
    expect(filtered).not.toContain("![chart]");
    expect(filtered).toContain("**bold**");

    const image = "data:image/png;base64,AAAA";
    for (const status of ["wait", "scaned", "verify_code"] as const) {
      const shaped = shapePublicQrWaitResponse({
        sessionKey: "sess-shape",
        status,
        connected: false,
        qrcodeImgContent: image,
      });
      expect(shaped.qrcode_url).toBe(image);
    }
    for (const status of ["expired", "confirmed", "superseded"] as const) {
      const shaped = shapePublicQrWaitResponse({
        sessionKey: "sess-shape",
        status,
        connected: status === "confirmed",
        qrcodeImgContent: image,
      });
      expect(shaped.qrcode_url).toBeUndefined();
    }
  });

  it("treats dispatched non-object iLink JSON as unknown and keeps object results", async () => {
    for (const body of [null, [1], true, 0, "hello"] as const) {
      const raw = JSON.stringify(body);
      await expect(
        getWeixinUpdates({
          baseUrl: "https://ilinkai.weixin.qq.com",
          botToken: "bot_tok",
          allowedDomains: ["ilinkai.weixin.qq.com"],
          checkResolvedIps: false,
          fetchImpl: async () =>
            new Response(raw, {
              status: 200,
              headers: { "content-type": "application/json" },
            }),
        }),
      ).rejects.toMatchObject({ kind: "http_error", requestSent: true });
      await expect(
        sendWeixinTextMessage(
          {
            baseUrl: "https://ilinkai.weixin.qq.com",
            cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
            allowedApiDomains: ["ilinkai.weixin.qq.com"],
            botToken: "bot_tok",
            toUserId: "wx_u1",
            contextToken: "ctx_valid",
            checkResolvedIps: false,
            fetchImpl: async () =>
              new Response(raw, {
                status: 200,
                headers: { "content-type": "application/json" },
              }),
          },
          "Hello",
        ),
      ).rejects.toMatchObject({ outcome: "unknown" });
    }
  });
});

// ===========================================================================
// §7.2 Gateway Test Checklist (Team Weixin Multi-Account — Batch B)
// ===========================================================================

describe("§7.2 #1 — Single-Instance Lease, Epoch Fencing, Strict Startup Order & 409 Immediate Stop", () => {
  it("waits when lease is held, completes mode=startup before ANY getupdates or I-6 claim, and serves /health while recovering", async () => {
    const events: string[] = [];
    const releaseRecover = defer<void>();
    const recoverEntered = defer<void>();
    const backend = createMockBackend();

    let acquireAttempts = 0;
    vi.mocked(backend.acquireGatewayLease).mockImplementation(async () => {
      acquireAttempts += 1;
      events.push(`lease_acquire:${acquireAttempts}`);
      if (acquireAttempts === 1) {
        return {
          granted: false,
          epoch: 0,
          expires_at: new Date(Date.now() + 45_000).toISOString(),
          retry_after_ms: 15,
        };
      }
      return {
        granted: true,
        epoch: 7,
        expires_at: new Date(Date.now() + 45_000).toISOString(),
      };
    });

    vi.mocked(backend.recoverState).mockImplementation(async (params) => {
      events.push(`recover:${params.mode}`);
      recoverEntered.resolve();
      await releaseRecover.promise;
      return {
        mode: "startup",
        inbound_events: [],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
        flushing_collect_buffers: [],
      };
    });

    vi.mocked(backend.reconcileWeixinAccounts).mockImplementation(async () => {
      events.push("weixin_reconcile");
      return {
        changed: true,
        enabled: true,
        max_concurrent_accounts: 50,
        accounts_revision: 1,
        accounts: [makeAccountSnapshot()],
      };
    });

    let getupdatesCalls = 0;
    const firstPollSeen = defer<void>();
    const wxAdapter = new WeixinChannelAdapter({
      backend,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      fetchImpl: async (input, init) => {
        if (String(input).includes("getupdates")) {
          getupdatesCalls += 1;
          events.push("getupdates");
          firstPollSeen.resolve();
          return await new Promise<Response>((_res, rej) => {
            init?.signal?.addEventListener("abort", () => rej(new Error("aborted")), {
              once: true,
            });
          });
        }
        return makeJsonResponse({ ret: 0 });
      },
    });

    const config = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      DALTON_WEIXIN_ENABLED: "true",
    });

    const app = createGatewayApp({
      config,
      backend,
      adapters: new Map<ChannelPlatform, typeof wxAdapter>([["weixin", wxAdapter]]),
      leaseAcquireRetryMs: 15,
    });

    const startPromise = app.start();
    try {
      await waitBounded(recoverEntered.promise, "recover entered");

      // While mode=startup is still in flight:
      // 1) Lease was acquired on retry with epoch=7
      expect(acquireAttempts).toBe(2);
      expect(backend.setGatewayEpoch).toHaveBeenCalledWith(7);
      // 2) Zero getupdates and zero reconcile before mode=startup completes
      expect(getupdatesCalls).toBe(0);
      expect(backend.reconcileWeixinAccounts).toHaveBeenCalledTimes(0);
      // 3) Attempting a delivery during startup recovery is blocked BEFORE calling I-6
      const blockedDelivery = await app.deliveryService.executeDelivery({
        deliveryKey: "blocked_during_startup",
        kind: "notice",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "too early",
      });
      expect(blockedDelivery.status).toBe("failed");
      expect(blockedDelivery.code).toBe("unavailable");
      expect(backend.authorizeAndClaimDelivery).toHaveBeenCalledTimes(0);

      // Complete mode=startup -> reconcile & getupdates start, and I-6 is unblocked
      releaseRecover.resolve();
      const { host, port } = await startPromise;
      await waitBounded(firstPollSeen.promise, "first getupdates after startup");

      const healthRes = await fetchGatewayServer(
        app,
        `http://${host}:${port}/health`,
        { headers: { "X-Service-Token": TEST_SERVICE_TOKEN } },
      );
      expect(healthRes.status).toBe(200);

      expect(events.slice(0, 5)).toEqual([
        "lease_acquire:1",
        "lease_acquire:2",
        "recover:startup",
        "weixin_reconcile",
        "getupdates",
      ]);
    } finally {
      releaseRecover.resolve();
      await app.stop();
    }
  });

  it("stops immediately on 409 stale_gateway_epoch without advancing cursor or writing I-7 settlement, and discards getupdates when local lease expires", async () => {
    const backend = createMockBackend();
    const leaseLostReasons: string[] = [];
    let staleEpochCallback: ((err: StaleGatewayEpochError) => void) | null = null;
    vi.mocked(backend.setOnStaleGatewayEpoch).mockImplementation((cb) => {
      staleEpochCallback = cb;
    });

    // Part A: 409 stale_gateway_epoch during I-1 stops polling, does NOT advance cursor, and triggers lease loss
    vi.mocked(backend.reconcileWeixinAccounts).mockResolvedValue({
      changed: true,
      enabled: true,
      max_concurrent_accounts: 50,
      accounts_revision: 1,
      accounts: [makeAccountSnapshot({ get_updates_buf: "cursor_orig" })],
    });

    const leaseLostDone = defer<void>();
    const lease = new GatewayLeaseManager({
      backend,
      ttlSeconds: 45,
      heartbeatIntervalMs: 60_000,
      onLeaseLost: (reason) => {
        leaseLostReasons.push(reason);
        leaseLostDone.resolve();
      },
    });
    await lease.acquireUntilGranted();

    let pollCount = 0;
    const wxAdapter = new WeixinChannelAdapter({
      backend,
      leaseManager: lease,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      retryDelayMs: 5,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      fetchImpl: async (input) => {
        if (String(input).includes("getupdates")) {
          pollCount += 1;
          return makeJsonResponse({
            ret: 0,
            get_updates_buf: "cursor_should_not_advance",
            msgs: [
              {
                message_id: "9001",
                seq: 1,
                from_user_id: "wx_alice",
                context_token: "ctx_9001",
                item_list: [{ type: 1, text_item: { text: "hello" } }],
              },
            ],
          });
        }
        return makeJsonResponse({ ret: 0 });
      },
    });

    wxAdapter.onInboundEvent(async () => {
      const err = new StaleGatewayEpochError("stale epoch on I-1", {
        status: 409,
        epoch: 1,
      });
      staleEpochCallback?.(err);
      throw err;
    });

    await wxAdapter.start();
    await waitBounded(leaseLostDone.promise, "lease lost on 409");
    await new Promise((r) => setTimeout(r, 25));

    expect(pollCount).toBe(1);
    expect(backend.updateWeixinAccountCursor).not.toHaveBeenCalled();
    expect(wxAdapter.getManagedAccounts()[0]?.getUpdatesBuf).toBe("cursor_orig");
    expect(leaseLostReasons).toEqual(["monitor_stale_epoch"]);
    await wxAdapter.stop();

    // Part B: 409 stale_gateway_epoch during DeliveryService preflight-send does NOT write I-7 settlement
    const h = createTestHarness();
    try {
      vi.mocked(h.backend.preflightWeixinSend).mockRejectedValueOnce(
        new StaleGatewayEpochError("stale epoch on preflight", { status: 409, epoch: 1 }),
      );
      await expect(
        h.app.deliveryService.executeDelivery({
          deliveryKey: "del_stale_409",
          kind: "agent_send",
          userId: USER_ID,
          platform: "weixin",
          target: "weixin:@me",
          text: "should not settle I-7",
        }),
      ).rejects.toBeInstanceOf(StaleGatewayEpochError);
      expect(h.backend.updateDeliveryProgress).not.toHaveBeenCalled();
      expect(h.weixinAdapter.sentTexts).toHaveLength(0);
    } finally {
      await h.cleanup();
    }

    // Part C: local lease expiring while getupdates is in-flight discards batch without calling I-1 or advancing cursor
    const backend2 = createMockBackend();
    vi.mocked(backend2.acquireGatewayLease).mockResolvedValue({
      granted: true,
      epoch: 2,
      // Expires in 2s (< 5s safety margin) so isLeaseLocallyValid() returns false on poll return
      expires_at: new Date(Date.now() + 2_000).toISOString(),
    });
    vi.mocked(backend2.reconcileWeixinAccounts).mockResolvedValue({
      changed: true,
      enabled: true,
      max_concurrent_accounts: 50,
      accounts_revision: 1,
      accounts: [makeAccountSnapshot({ get_updates_buf: "cursor_before_expiry" })],
    });
    const expiringLease = new GatewayLeaseManager({
      backend: backend2,
      ttlSeconds: 45,
      heartbeatIntervalMs: 60_000,
    });
    await expiringLease.acquireUntilGranted();
    expect(expiringLease.isLeaseLocallyValid()).toBe(false);

    const inboundSpy = vi.fn(async () => undefined);
    const wxAdapter2 = new WeixinChannelAdapter({
      backend: backend2,
      leaseManager: expiringLease,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      fetchImpl: async () =>
        makeJsonResponse({
          ret: 0,
          get_updates_buf: "cursor_after_expiry",
          msgs: [
            {
              message_id: "9002",
              from_user_id: "wx_alice",
              item_list: [{ type: 1, text_item: { text: "discarded" } }],
            },
          ],
        }),
    });
    wxAdapter2.onInboundEvent(inboundSpy);
    await wxAdapter2.start();
    await new Promise((r) => setTimeout(r, 30));
    expect(inboundSpy).not.toHaveBeenCalled();
    expect(backend2.updateWeixinAccountCursor).not.toHaveBeenCalled();
    await wxAdapter2.stop();
  });
});

describe("§7.2 #2 — Outbound preflight-send, defer vs deny, uncertain unknown, and D16 one-time reply", () => {
  it("calls preflight-send before every text chunk, before CDN upload, and again after CDN upload before sendmessage; aborts sendmessage if account changes mid-upload", async () => {
    const h = createTestHarness();
    const paused = pausedCdnFetch();
    const lease = new GatewayLeaseManager({
      backend: h.backend,
      ttlSeconds: 45,
      heartbeatIntervalMs: 60_000,
    });
    const wxAdapter = new WeixinChannelAdapter({
      backend: h.backend,
      leaseManager: lease,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
      checkResolvedIps: false,
      fetchImpl: paused.fetchImpl,
    });
    const textFetchCalls: string[] = [];
    const textWxAdapter = new WeixinChannelAdapter({
      backend: h.backend,
      leaseManager: lease,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      fetchImpl: async (input, init) => {
        textFetchCalls.push(String(input));
        expect(headerValue(init, "authorization")).toBe("Bearer bot_tok_live");
        return makeJsonResponse({ ret: 0, message_id: `wx_m_${textFetchCalls.length}` });
      },
    });
    try {
      const epoch = await lease.acquireUntilGranted();
      h.backend.setGatewayEpoch(epoch);
      await wxAdapter.start();
      await textWxAdapter.start();

      const app = createGatewayApp({
        config: h.config,
        backend: h.backend,
        leaseManager: lease,
        adapters: new Map<ChannelPlatform, typeof wxAdapter>([["weixin", wxAdapter]]),
      });

      // 1) Multi-chunk text calls preflight-send for chunk 0 and chunk 1
      const preflightChunks: number[] = [];
      const textApp = createGatewayApp({
        config: h.config,
        backend: h.backend,
        leaseManager: lease,
        adapters: new Map<ChannelPlatform, typeof textWxAdapter>([
          ["weixin", textWxAdapter],
        ]),
      });

      vi.mocked(h.backend.preflightWeixinSend).mockImplementation(
        async (_key, req) => {
          preflightChunks.push(req.chunk_index);
          return makePreflightAllow({ chunk_index: req.chunk_index });
        },
      );

      const twoChunksText = `${"A".repeat(1900)}\n${"B".repeat(1900)}`;
      const textRes = await textApp.deliveryService.executeDelivery({
        deliveryKey: "del_two_chunks",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: twoChunksText,
      });
      expect(textRes.status).toBe("sent");
      expect(preflightChunks).toEqual([0, 1]);
      expect(textFetchCalls).toHaveLength(2);

      // 2) File delivery calls preflight-send before CDN upload AND again after CDN upload before sendmessage.
      // When the 2nd preflight reports a changed bot_generation, sendmessage MUST NOT be called.
      let filePreflightCount = 0;
      vi.mocked(h.backend.preflightWeixinSend).mockImplementation(async () => {
        filePreflightCount += 1;
        if (filePreflightCount === 1) {
          return makePreflightAllow({
            ilink_bot_id: "bot_live_1",
            bot_generation: 5,
            bot_token: "bot_tok_v5",
          });
        }
        // Account rotated during CDN upload
        return makePreflightAllow({
          ilink_bot_id: "bot_live_1",
          bot_generation: 6,
          bot_token: "bot_tok_v6",
        });
      });

      const fileSendPromise = app.deliveryService.executeDelivery({
        deliveryKey: "del_file_rotated",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "",
        files: [{ path: "notes/memo.pdf", workspace_id: HOME_WS_ID }],
      });

      await waitBounded(paused.uploadEntered.promise, "cdn upload entered");
      expect(filePreflightCount).toBe(1);
      expect(paused.sendMessages).toHaveLength(0);

      paused.releaseUpload.resolve();
      const fileRes = await fileSendPromise;
      expect(filePreflightCount).toBe(2);
      expect(paused.sendMessages).toHaveLength(0);
      expect(fileRes.status).toBe("failed");
    } finally {
      paused.releaseUpload.resolve();
      await textWxAdapter.stop();
      await wxAdapter.stop();
      await lease.stopAndRelease();
      await h.cleanup();
    }
  });

  it("distinguishes preflight defer (non-terminal deferred + retryable) vs deny (terminal failed) vs source_generation_unknown (unknown)", async () => {
    const h = createTestHarness();
    try {
      // 1) Decision #4: defer (retryable) -> settles I-7 as non-terminal "deferred" with error_code="roster_lookup_error",
      //    and a subsequent retry succeeds when preflight allows.
      vi.mocked(h.backend.preflightWeixinSend)
        .mockResolvedValueOnce({
          authorized: false,
          decision: "defer",
          retryable: true,
          deny_code: "roster_lookup_error",
          message: "Temporary roster lookup failure",
        })
        .mockResolvedValueOnce(makePreflightAllow());
      const deferRes = await h.app.deliveryService.executeDelivery({
        deliveryKey: "del_preflight_defer",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "hello",
      });
      expect(deferRes.status).toBe("deferred");
      expect(deferRes.code).toBe("unavailable");
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "del_preflight_defer",
        status: "deferred",
        error_code: "roster_lookup_error",
      });
      expect(h.weixinAdapter.sentTexts).toHaveLength(0);

      // Re-delivery after backend re-queues deferred item succeeds
      const retryRes = await h.app.deliveryService.executeDelivery({
        deliveryKey: "del_preflight_defer",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "hello",
      });
      expect(retryRes.status).toBe("sent");
      expect(h.weixinAdapter.sentTexts).toHaveLength(1);

      // 2) deny (terminal): e.g. account_unbound -> code not_allowed
      vi.mocked(h.backend.preflightWeixinSend).mockResolvedValueOnce({
        authorized: false,
        decision: "deny",
        retryable: false,
        deny_code: "account_unbound",
        message: "Weixin account unbound",
      });
      const denyRes = await h.app.deliveryService.executeDelivery({
        deliveryKey: "del_preflight_deny",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "hello",
      });
      expect(denyRes.status).toBe("failed");
      expect(denyRes.code).toBe("not_allowed");
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "del_preflight_deny",
        status: "failed",
        error_code: "account_unbound",
      });

      // 3) source_generation_unknown -> status unknown
      vi.mocked(h.backend.preflightWeixinSend).mockResolvedValueOnce({
        authorized: false,
        decision: "deny",
        retryable: false,
        deny_code: "source_generation_unknown",
      });
      const unknownRes = await h.app.deliveryService.executeDelivery({
        deliveryKey: "del_preflight_unknown",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "hello",
      });
      expect(unknownRes.status).toBe("unknown");
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "del_preflight_unknown",
        status: "unknown",
        error_code: "source_generation_unknown",
      });
    } finally {
      await h.cleanup();
    }
  });

  it("delivers D16 one_time_weixin_reply directly with inbound context_token without preflight-send on conflict auto-unbind and /unbind, deduplicating retries", async () => {
    const h = createTestHarness();
    try {
      // 1) I-1 conflict auto-unbind returns one_time_weixin_reply
      const conflictEnvelope = {
        account_id: "wx_acc_1",
        ilink_bot_id: "bot_live_1",
        bot_generation: 5,
        bot_token: "bot_tok_live",
        base_url: "https://ilinkai.weixin.qq.com",
        recipient_id: "wx_other_person",
        context_token: "ctx_inbound_conflict",
        text: "该微信号已绑定其他成员，已自动解绑。",
      };
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementation(
        async (req) => ({
          ...emptyInbound(req),
          deny_reason: "ilink_user_conflict_auto_unbound",
          one_time_weixin_reply: conflictEnvelope,
        }),
      );

      await h.weixinAdapter.emitInbound(
        weixinDm("bot_live_1:conflict_1", "hi", {
          platformUserId: "wx_other_person",
          weixinContextToken: "ctx_inbound_conflict",
        }),
      );
      // Duplicate replay of same message must NOT resend one-time reply
      await h.weixinAdapter.emitInbound(
        weixinDm("bot_live_1:conflict_1", "hi", {
          platformUserId: "wx_other_person",
          weixinContextToken: "ctx_inbound_conflict",
        }),
      );

      expect(h.weixinAdapter.sentOneTimeReplies).toHaveLength(1);
      expect(h.weixinAdapter.sentOneTimeReplies[0]?.envelope).toEqual(
        conflictEnvelope,
      );
      expect(h.backend.preflightWeixinSend).not.toHaveBeenCalled();
      expect(h.backend.authorizeAndClaimDelivery).not.toHaveBeenCalled();

      // 2) Weixin /unbind invokes I-2 directly with all 4 required fields and sends D16 one_time_weixin_reply
      const unbindEnvelope = {
        account_id: "wx_acc_1",
        ilink_bot_id: "bot_live_1",
        bot_generation: 5,
        bot_token: "bot_tok_live",
        base_url: "https://ilinkai.weixin.qq.com",
        recipient_id: "wx_alice",
        context_token: "ctx_inbound_unbind",
        text: "已解除当前微信绑定。",
      };
      vi.mocked(h.backend.consumeBindCode).mockResolvedValueOnce({
        ok: true,
        action: "unbind",
        user_id: USER_ID,
        platform: "weixin",
        platform_user_id: "wx_alice",
        accounts_revision: 9,
        one_time_weixin_reply: unbindEnvelope,
      });

      const i1CallsBeforeUnbind = vi.mocked(
        h.backend.registerAndResolveInbound,
      ).mock.calls.length;

      await h.weixinAdapter.emitInbound(
        weixinDm("bot_live_1:unbind_1", "/unbind", {
          weixinAccountId: "wx_acc_1",
          weixinBotId: "bot_live_1",
          weixinBotGeneration: 5,
          weixinContextToken: "ctx_inbound_unbind",
        }),
      );

      // Must NOT have called I-1 before I-2 for Weixin /unbind
      expect(
        vi.mocked(h.backend.registerAndResolveInbound).mock.calls.length,
      ).toBe(i1CallsBeforeUnbind);
      expect(h.backend.consumeBindCode).toHaveBeenCalledWith(
        expect.objectContaining({
          action: "unbind",
          platform: "weixin",
          platform_user_id: "wx_alice",
          weixin_account_id: "wx_acc_1",
          weixin_bot_id: "bot_live_1",
          weixin_bot_generation: 5,
        }),
      );
      expect(h.weixinAdapter.sentOneTimeReplies).toHaveLength(2);
      expect(h.weixinAdapter.sentOneTimeReplies[1]?.envelope).toEqual(
        unbindEnvelope,
      );
      expect(h.backend.preflightWeixinSend).not.toHaveBeenCalled();
    } finally {
      await h.cleanup();
    }
  });
});

describe("§7.2 #3 & Codex ④ — Startup Recovery & Collection Mode (/collect, /done, /cancel, /help)", () => {
  it("recovers normal 2-attachment Weixin row using decrypted_weixin_attachments (skips #1, uploads #2, runs turn)", async () => {
    const h = createTestHarness();
    try {
      h.weixinAdapter.setAttachment("bot_1:m_2att:att:1", {
        content: Buffer.from("second attachment content", "utf8"),
        filename: "report_2.pdf",
        mimeType: "application/pdf",
      });

      const row: RecoverInboundEventRow = {
        platform: "weixin",
        message_id: "bot_1:m_2att",
        platform_user_id: "wx_alice",
        dalton_user_id: USER_ID,
        chat_type: "dm",
        chat_address: "weixin:@me",
        thread_scope_key: "dm:wx_alice",
        request_key: "req_2att",
        retry_request_key: null,
        resubmitted_request_key: null,
        steered_into_run_id: null,
        thread_id: null,
        run_id: null,
        turn_index: null,
        workspace_id: HOME_WS_ID,
        external_thread_id: null,
        generation: 1,
        status: "received",
        fallback_pending: false,
        sent_to_current_count: 0,
        uploaded_attachments: [
          {
            filename: "report_1.pdf",
            workspace_path: "uploads/weixin/bot_1_m_2att/report_1.pdf",
            mime_type: "application/pdf",
            size_bytes: 128,
          },
        ],
        // raw_payload has stripped secrets (no aesKey/encryptQueryParam)
        raw_payload: { text: "Analyze both files" },
        decrypted_weixin_attachments: [
          {
            id: "bot_1:m_2att:att:0",
            kind: "file",
            filename: "report_1.pdf",
            mimeType: "application/pdf",
            encryptQueryParam: "enc_1",
          },
          {
            id: "bot_1:m_2att:att:1",
            kind: "file",
            filename: "report_2.pdf",
            mimeType: "application/pdf",
            encryptQueryParam: "enc_2",
          },
        ],
        weixin_account_id: "wx_acc_1",
        weixin_context_bot_id: "bot_1",
        weixin_context_bot_generation: 1,
      };

      vi.mocked(h.backend.registerAndResolveInbound).mockImplementationOnce(
        async (req) => ({
          ...emptyInbound(req),
          existing_status: "received",
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
          uploaded_attachments: row.uploaded_attachments,
        }),
      );

      vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
        mode: "startup",
        inbound_events: [row],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
        flushing_collect_buffers: [],
      });

      await h.app.orchestrator.runStartupRecovery();

      // Recovery idempotent I-1 forwards Weixin identity fields
      expect(h.backend.registerAndResolveInbound).toHaveBeenCalledWith(
        expect.objectContaining({
          platform: "weixin",
          message_id: "bot_1:m_2att",
          weixin_account_id: "wx_acc_1",
          weixin_bot_id: "bot_1",
          weixin_bot_generation: 1,
        }),
      );

      // Skipped already-uploaded #1, uploaded ONLY #2 (with Weixin AbortSignal), then started turn
      expect(h.backend.uploadWorkspaceFile).toHaveBeenCalledTimes(1);
      expect(h.backend.uploadWorkspaceFile).toHaveBeenCalledWith(
        USER_ID,
        HOME_WS_ID,
        expect.stringContaining("report_2.pdf"),
        "report_2.pdf",
        expect.any(Buffer),
        "application/pdf",
        expect.any(AbortSignal),
      );
      expect(h.backend.createThreadMessages).toHaveBeenCalledTimes(1);
    } finally {
      await h.cleanup();
    }
  });

  it("recovers collect-buffer inbound rows without starting a turn, finalizes flushing_collect_buffers into 1 synthetic turn, and skips cancelled collect buffers", async () => {
    const h = createTestHarness();
    try {
      h.weixinAdapter.setAttachment("bot_1:m_col_1:att:0", {
        content: Buffer.from("collect file 1", "utf8"),
        filename: "q1.pdf",
        mimeType: "application/pdf",
      });
      h.weixinAdapter.setAttachment("bot_1:m_col_cancelled:att:0", {
        content: Buffer.from("should never download", "utf8"),
        filename: "cancelled.pdf",
        mimeType: "application/pdf",
      });

      const activeCollectRow: RecoverInboundEventRow = {
        platform: "weixin",
        message_id: "bot_1:m_col_1",
        platform_user_id: "wx_alice",
        dalton_user_id: USER_ID,
        chat_type: "dm",
        chat_address: "weixin:@me",
        thread_scope_key: "dm:wx_alice",
        request_key: "req_col_1",
        retry_request_key: null,
        resubmitted_request_key: null,
        steered_into_run_id: null,
        thread_id: null,
        run_id: null,
        turn_index: null,
        workspace_id: HOME_WS_ID,
        external_thread_id: null,
        generation: 1,
        status: "received",
        fallback_pending: false,
        sent_to_current_count: 0,
        uploaded_attachments: [],
        raw_payload: { text: "Q1 report" },
        decrypted_weixin_attachments: [
          {
            id: "bot_1:m_col_1:att:0",
            kind: "file",
            filename: "q1.pdf",
            mimeType: "application/pdf",
            encryptQueryParam: "enc_q1",
          },
        ],
        collect_buffer_id: "buf_active",
        collect_buffer_status: "collecting",
        weixin_account_id: "wx_acc_1",
      };

      const cancelledCollectRow: RecoverInboundEventRow = {
        ...activeCollectRow,
        message_id: "bot_1:m_col_cancelled",
        request_key: "req_col_cancelled",
        collect_buffer_id: "buf_cancelled",
        collect_buffer_status: "cancelled",
        decrypted_weixin_attachments: [
          {
            id: "bot_1:m_col_cancelled:att:0",
            kind: "file",
            filename: "cancelled.pdf",
            mimeType: "application/pdf",
            encryptQueryParam: "enc_cancelled",
          },
        ],
      };

      vi.mocked(h.backend.finalizeWeixinCollectBuffer).mockResolvedValueOnce({
        ok: true,
        status: "done",
        // Decision #5: mirrors real Batch A `_serialize_synthetic_collect_inbound`
        // (text & platform_user_id live inside `raw_payload`, NOT top-level).
        flushed_inbound: {
          platform: "weixin",
          message_id: "collect:buf_flushing",
          request_key: "req_collect_flush_1",
          dalton_user_id: USER_ID,
          thread_scope_key: "dm:wx_alice",
          workspace_id: HOME_WS_ID,
          external_thread_id: null,
          generation: 1,
          raw_payload: {
            text: "Compare Q1 and Q2 reports",
            platform_user_id: "wx_alice",
          },
          uploaded_attachments: [
            {
              filename: "q1.pdf",
              workspace_path: "uploads/weixin/q1.pdf",
              mime_type: "application/pdf",
              size_bytes: 100,
            },
          ],
          weixin_account_id: "wx_acc_1",
          weixin_bot_generation: 1,
          status: "pending",
        },
      });

      vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
        mode: "startup",
        inbound_events: [activeCollectRow, cancelledCollectRow],
        flushing_collect_buffers: [
          {
            buffer_id: "buf_flushing",
            dalton_user_id: USER_ID,
            weixin_account_id: "wx_acc_1",
            status: "flushing",
          },
        ],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
      });

      await h.app.orchestrator.runStartupRecovery();

      // Active collect row uploaded q1.pdf and registered attachments — NEVER started a turn for itself
      expect(h.backend.uploadWorkspaceFile).toHaveBeenCalledTimes(1);
      expect(h.backend.registerWeixinCollectItemAttachments).toHaveBeenCalledTimes(
        1,
      );
      expect(h.backend.registerWeixinCollectItemAttachments).toHaveBeenCalledWith(
        "buf_active",
        "bot_1:m_col_1",
        expect.objectContaining({
          uploaded_attachments: [
            expect.objectContaining({ filename: "q1.pdf" }),
          ],
        }),
      );

      // Flushing buffer finalized into exactly ONE synthetic turn
      expect(h.backend.finalizeWeixinCollectBuffer).toHaveBeenCalledWith(
        "buf_flushing",
      );
      expect(h.backend.createThreadMessages).toHaveBeenCalledTimes(1);
      expect(h.backend.createCalls[0]?.payload.request_key).toBe(
        "req_collect_flush_1",
      );
    } finally {
      await h.cleanup();
    }
  });

  it("Codex ④: /cancel mid-upload aborts unstarted attachments and discards in-flight upload without calling 补登", async () => {
    const h = createTestHarness();
    try {
      const download1Started = defer<void>();
      const releaseDownload1 = defer<void>();
      const downloadedIds: string[] = [];

      h.weixinAdapter.setDownloadAttachmentDelegate(async (desc) => {
        downloadedIds.push(desc.id);
        if (desc.id === "att_1") {
          download1Started.resolve();
          await waitBounded(releaseDownload1.promise, "release att_1 download");
        }
        return {
          content: Buffer.from(`content_${desc.id}`, "utf8"),
          filename: desc.filename,
          mimeType: desc.mimeType,
        };
      });

      vi.mocked(h.backend.registerAndResolveInbound).mockImplementationOnce(
        async (req) => ({
          ...emptyInbound(req),
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
          collection_mode: "appended",
          collect_buffer_id: "buf_mid_cancel",
          collect_buffer_status: "collecting",
        }),
      );

      vi.mocked(h.backend.sessionCommand).mockResolvedValueOnce({
        ok: true,
        linked: true,
        allowed: true,
        dalton_user_id: USER_ID,
        collect_buffer_id: "buf_mid_cancel",
        cancelled_buffer_ids: ["buf_mid_cancel"],
        collect_status: "cancelled",
        reply_text: "已取消材料收集窗口。",
        command_reply_pending: true,
      });

      // Emit a 2-attachment message appended to buf_mid_cancel
      await h.weixinAdapter.emitInbound(
        weixinDm("bot_1:m_two_files", "files", {
          attachments: [
            { id: "att_1", filename: "a.pdf", mimeType: "application/pdf" },
            { id: "att_2", filename: "b.pdf", mimeType: "application/pdf" },
          ],
        }),
      );
      await waitBounded(download1Started.promise, "att_1 download started");

      // User sends /cancel while att_1 is still downloading in-flight
      await h.weixinAdapter.emitInbound(weixinDm("bot_1:m_cancel", "/cancel"));

      // Release in-flight download of att_1 -> must discard result, never start att_2, never upload or register
      releaseDownload1.resolve();
      await new Promise((r) => setTimeout(r, 30));

      expect(downloadedIds).toEqual(["att_1"]);
      expect(h.backend.uploadWorkspaceFile).not.toHaveBeenCalled();
      expect(
        h.backend.registerWeixinCollectItemAttachments,
      ).not.toHaveBeenCalled();
      expect(h.backend.createThreadMessages).not.toHaveBeenCalled();
    } finally {
      await h.cleanup();
    }
  });

  it("isolates Weixin-only slash commands (/collect, /收集, /done, /完成, /cancel, /取消, /help) from Feishu and Discord", () => {
    expect(parseSlashCommand("/collect", "dm", "weixin")).toMatchObject({
      kind: "session",
      cmd: "collect",
    });
    expect(parseSlashCommand("/收集", "dm", "weixin")).toMatchObject({
      kind: "session",
      cmd: "collect",
    });
    expect(
      parseSlashCommand("/done 帮我对比三份财报", "dm", "weixin"),
    ).toMatchObject({
      kind: "session",
      cmd: "done",
      commandArg: "帮我对比三份财报",
    });
    expect(parseSlashCommand("/完成 总结重点", "dm", "weixin")).toMatchObject({
      kind: "session",
      cmd: "done",
      commandArg: "总结重点",
    });
    expect(parseSlashCommand("/cancel", "dm", "weixin")).toMatchObject({
      kind: "session",
      cmd: "cancel",
    });
    expect(parseSlashCommand("/取消", "dm", "weixin")).toMatchObject({
      kind: "session",
      cmd: "cancel",
    });
    expect(parseSlashCommand("/help", "dm", "weixin")).toMatchObject({
      kind: "session",
      cmd: "help",
    });

    // Feishu & Discord MUST treat /collect, /done, /cancel, /help identically to before (shortcut_name)
    for (const plat of ["feishu", "discord", undefined] as const) {
      expect(parseSlashCommand("/collect", "dm", plat)).toMatchObject({
        kind: "session",
        cmd: "workspace_switch",
        targetKind: "shortcut_name",
        workspaceQuery: "collect",
      });
      expect(parseSlashCommand("/done", "dm", plat)).toMatchObject({
        kind: "session",
        cmd: "workspace_switch",
        targetKind: "shortcut_name",
        workspaceQuery: "done",
      });
    }
  });
});

describe("§7.2 #4 — Merged-Forward & Unsupported Item Handling", () => {
  it("preserves text/attachments and appends system note on mixed message, sends conditional hint when usable token exists, and stays silent without token", async () => {
    // 1) Mixed message: text + image + unsupported item type 49
    const mixedEvent = parseWeixinInboundMessage(
      {
        message_id: "5001",
        from_user_id: "wx_alice",
        context_token: "ctx_5001",
        item_list: [
          { type: 1, text_item: { text: "Here is my note" } },
          {
            type: 2,
            image_item: {
              aeskey: "00112233445566778899aabbccddeeff",
              media: { encrypt_query_param: "enc_5001" },
            },
          },
          { type: 49 },
        ],
      },
      { accountId: "wx_acc_1", ilinkBotId: "bot_1", botGeneration: 1 },
    );
    expect(mixedEvent.text).toContain("Here is my note");
    expect(mixedEvent.text).toContain(WEIXIN_MIXED_UNSUPPORTED_NOTE);
    expect(mixedEvent.attachments).toHaveLength(1);
    expect(mixedEvent.unsupportedItemTypes).toEqual([49]);

    // 2) Pure unsupported message: no text/attachments, unsupportedItemTypes=[49]
    const pureUnsupported = parseWeixinInboundMessage(
      {
        message_id: "5002",
        from_user_id: "wx_alice",
        context_token: "ctx_5002",
        item_list: [{ type: 49 }],
      },
      { accountId: "wx_acc_1", ilinkBotId: "bot_1", botGeneration: 1 },
    );
    expect(pureUnsupported.text).toBe("");
    expect(pureUnsupported.attachments).toHaveLength(0);
    expect(pureUnsupported.unsupportedItemTypes).toEqual([49]);

    const h = createTestHarness();
    try {
      // Case A: has_usable_weixin_context_token === true -> sends exact §4.4 guidance hint, deduped on replay
      vi.mocked(h.backend.registerAndResolveInbound)
        .mockImplementationOnce(async (req) => ({
          ...emptyInbound(req),
          existing_status: "ignored",
          linked: true,
          allowed: false,
          dalton_user_id: USER_ID,
          deny_reason: "unsupported_merged_forward",
          has_usable_weixin_context_token: true,
        }))
        .mockImplementationOnce(async (req) => ({
          ...emptyInbound(req),
          existing_status: "ignored",
          linked: true,
          allowed: false,
          dalton_user_id: USER_ID,
          deny_reason: "unsupported_merged_forward",
          has_usable_weixin_context_token: true,
        }));
      await h.weixinAdapter.emitInbound(pureUnsupported);
      // Duplicate replay of exact same merged-forward message MUST NOT send a second hint
      await h.weixinAdapter.emitInbound(pureUnsupported);
      expect(h.weixinAdapter.sentTexts).toHaveLength(1);
      expect(h.weixinAdapter.sentTexts[0]?.text).toBe(
        "暂无法直接解析微信『合并转发』的聊天记录，请改用『逐条转发』（发 `/collect` 开始收集、转完发 `/done` 统一分析）或发送聊天长截图。",
      );
      expect(h.backend.createThreadMessages).not.toHaveBeenCalled();

      // Case B: has_usable_weixin_context_token === false -> registers via I-1 (archived silently), 0 outbound sends
      const i1CountBeforeSilent = vi.mocked(
        h.backend.registerAndResolveInbound,
      ).mock.calls.length;
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementationOnce(
        async (req) => ({
          ...emptyInbound(req),
          existing_status: "ignored",
          linked: true,
          allowed: false,
          dalton_user_id: USER_ID,
          deny_reason: "unsupported_merged_forward",
          has_usable_weixin_context_token: false,
        }),
      );
      await h.weixinAdapter.emitInbound({
        ...pureUnsupported,
        messageId: "bot_1:5003",
      });
      expect(
        vi.mocked(h.backend.registerAndResolveInbound).mock.calls.length,
      ).toBe(i1CountBeforeSilent + 1);
      expect(h.backend.registerAndResolveInbound).toHaveBeenLastCalledWith(
        expect.objectContaining({
          platform: "weixin",
          message_id: "bot_1:5003",
          unsupported_item_types: [49],
        }),
      );
      expect(h.weixinAdapter.sentTexts).toHaveLength(1);
      expect(h.backend.createThreadMessages).not.toHaveBeenCalled();
    } finally {
      await h.cleanup();
    }
  });
});

describe("Multi-Account Reconcile, Layer-2/3 Switches, Sync Nudge & §4.3 Log Redaction", () => {
  it("handles Layer-2 404 probe, Layer-3 enabled=false pause, max_concurrent_accounts cap, and /internal/weixin/accounts/sync nudge", async () => {
    const backend = createMockBackend();
    vi.mocked(backend.reconcileWeixinAccounts)
      // 1) Initial reconcile with 3 accounts, capped to max_concurrent_accounts=2
      .mockResolvedValueOnce({
        changed: true,
        enabled: true,
        max_concurrent_accounts: 2,
        accounts_revision: 1,
        accounts: [
          makeAccountSnapshot({ account_id: "acc_1", ilink_bot_id: "bot_1", bot_token: "tok_1" }),
          makeAccountSnapshot({ account_id: "acc_2", ilink_bot_id: "bot_2", bot_token: "tok_2" }),
          makeAccountSnapshot({ account_id: "acc_3", ilink_bot_id: "bot_3", bot_token: "tok_3" }),
        ],
      })
      // 2) Runtime toggle disabled (Layer 3) -> stops all pollers
      .mockResolvedValueOnce({
        changed: true,
        enabled: false,
        max_concurrent_accounts: 2,
        accounts_revision: 2,
        accounts: [],
      })
      // 3) Backend routes disabled (Layer 2: 404) -> stops all pollers
      .mockRejectedValueOnce(new BackendHttpError("not found", 404, null));

    const adapter = new WeixinChannelAdapter({
      backend,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      reconcileIntervalMs: 60_000,
      probeInterval404Ms: 300_000,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      fetchImpl: async (_input, init) =>
        await new Promise<Response>((_res, rej) => {
          init?.signal?.addEventListener("abort", () => rej(new Error("aborted")), {
            once: true,
          });
        }),
    });
    adapter.onInboundEvent(async () => undefined);

    const invalidated: Array<{ accountId: string; reason: string }> = [];
    adapter.onWeixinAccountInvalidated((info) => invalidated.push(info));

    await adapter.start();
    try {
      expect(adapter.getManagedAccounts()).toHaveLength(2);
      expect(adapter.getManagedAccounts().map((a) => a.accountId)).toEqual([
        "acc_1",
        "acc_2",
      ]);

      // Layer 3 enabled:false stops all pollers
      await adapter.reconcileNow("test_disable");
      expect(adapter.getManagedAccounts()).toHaveLength(0);
      expect(invalidated.map((i) => i.reason)).toEqual([
        "feature_disabled",
        "feature_disabled",
      ]);

      // Layer 2 404 marks routes disabled cleanly without throwing
      await adapter.reconcileNow("test_404");
      expect(adapter.isConnected()).toBe(false);
    } finally {
      await adapter.stop();
    }

    // Verify HTTP route POST /internal/weixin/accounts/sync triggers single-flight sync
    const h = createTestHarness();
    try {
      const { host, port } = await h.app.start();
      const syncRes = await fetchGatewayServer(
        h.app,
        `http://${host}:${port}/internal/weixin/accounts/sync`,
        {
          method: "POST",
          headers: {
            "X-Service-Token": TEST_SERVICE_TOKEN,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ accounts_revision: 12, reason: "qr_confirmed" }),
        },
      );
      expect(syncRes.status).toBe(200);
      expect(await syncRes.json()).toEqual({ ok: true, accepted_revision: 12 });
      expect(h.weixinAdapter.accountsSyncCalls).toEqual([
        { targetRevision: 12, reason: "qr_confirmed" },
      ]);
    } finally {
      await h.cleanup();
    }
  });

  it("enforces §4.3 credential redaction in logs, iLink business errors, and HTTP errors", async () => {
    // 1) iLink business error does NOT include raw errmsg
    try {
      await sendWeixinTextMessage(
        {
          baseUrl: "https://ilinkai.weixin.qq.com",
          cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          allowedApiDomains: ["ilinkai.weixin.qq.com"],
          botToken: "super_secret_bot_token",
          toUserId: "wx_u1",
          contextToken: "super_secret_ctx_token",
          checkResolvedIps: false,
          fetchImpl: async () =>
            makeJsonResponse({
              ret: -2,
              errcode: 40001,
              errmsg: "RAW_SECRET_ERRMSG_MUST_NOT_LEAK",
            }),
        },
        "Hello",
      );
      expect.unreachable("Expected iLink business error to reject");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      expect(msg).toContain("ret=-2");
      expect(msg).toContain("errcode=40001");
      expect(msg).not.toContain("RAW_SECRET_ERRMSG_MUST_NOT_LEAK");
    }

    // 2) Logger redacts bot_token, context_token, qrcode, aes_key, encrypt_query_param, typing_ticket
    const loggedLines: string[] = [];
    const logger = createSubsystemLogger("weixin-test", {
      sink: {
        info: (l) => loggedLines.push(l),
        warn: (l) => loggedLines.push(l),
        error: (l) => loggedLines.push(l),
      },
    });
    logger.info("account sync", {
      account_id: "acc_1",
      bot_token: "SECRET_BOT_TOK",
      weixin_context_token: "SECRET_CTX_TOK",
      qrcode: "SECRET_QR_CODE",
      aes_key: "SECRET_AES_KEY",
      encrypt_query_param: "SECRET_EQP",
      typing_ticket: "SECRET_TYPING_TICKET",
    });
    expect(loggedLines[0]).toContain("acc_1");
    for (const secret of [
      "SECRET_BOT_TOK",
      "SECRET_CTX_TOK",
      "SECRET_QR_CODE",
      "SECRET_AES_KEY",
      "SECRET_EQP",
      "SECRET_TYPING_TICKET",
    ]) {
      expect(loggedLines[0]).not.toContain(secret);
    }

    expect(
      redactCredentialString(
        '{"bot_token":"tok123","qrcode_img_content":"qr456","aesKeyHex":"deadbeef"}',
      ),
    ).not.toMatch(/tok123|qr456|deadbeef/);
    expect(formatSafeSessionKeyPrefix("12345678-abcd-efgh")).toBe("12345678");
  });
});

// ===========================================================================
// Batch B Round 2 — Review Fixes & Regressions (Decisions #1 ~ #17)
// ===========================================================================

describe("Batch B Round 2 — Lease Abort, Pre-Lease 503, Deferred/Unknown, Collect Concurrency, Self-Heal & Pools", () => {
  it("Decision #1: synchronously aborts hanging sendmessage on lease loss without waiting for poll loop, rejects post-loss send/QR, and lets 2nd instance resume from unadvanced cursor", async () => {
    const backend = createMockBackend();
    const sendEntered = defer<void>();
    let sendSignalAborted = false;
    const pollBlocked = defer<void>();

    vi.mocked(backend.reconcileWeixinAccounts).mockResolvedValue({
      changed: true,
      enabled: true,
      max_concurrent_accounts: 50,
      accounts_revision: 1,
      accounts: [makeAccountSnapshot({ get_updates_buf: "cursor_v0" })],
    });

    const leaseLostDone = defer<void>();
    const lease = new GatewayLeaseManager({
      backend,
      ttlSeconds: 45,
      heartbeatIntervalMs: 60_000,
      onLeaseLost: () => {
        wxAdapter.abortAllInFlight();
        leaseLostDone.resolve();
      },
    });
    await lease.acquireUntilGranted();

    const wxAdapter = new WeixinChannelAdapter({
      backend,
      leaseManager: lease,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      fetchImpl: async (input, init) => {
        const url = String(input);
        if (url.includes("sendmessage")) {
          sendEntered.resolve();
          return await new Promise<Response>((_res, rej) => {
            init?.signal?.addEventListener(
              "abort",
              () => {
                sendSignalAborted = true;
                rej(new Error("aborted"));
              },
              { once: true },
            );
          });
        }
        if (url.includes("getupdates")) {
          return makeJsonResponse({
            ret: 0,
            get_updates_buf: "cursor_v1_should_not_commit",
            msgs: [
              {
                message_id: "7001",
                from_user_id: "wx_alice",
                item_list: [{ type: 1, text_item: { text: "stuck inbound" } }],
              },
            ],
          });
        }
        return makeJsonResponse({ ret: 0 });
      },
    });
    // Inbound handler hangs on pollBlocked (simulating long I-3/turn), yet sendmessage MUST still abort immediately
    wxAdapter.onInboundEvent(async () => {
      await pollBlocked.promise;
    });

    await wxAdapter.start();

    const hangingSend = wxAdapter.sendTextChunk({
      targetAddress: "weixin:@me",
      recipientPlatformUserId: "wx_alice",
      text: "in-flight during lease loss",
      chunkIndex: 0,
      totalChunks: 1,
      weixinPreflight: makePreflightAllow(),
    });

    await waitBounded(sendEntered.promise, "sendmessage entered");

    // Trigger lease loss while poll handler is STILL hanging
    lease.notifyLeaseLost("test_409_stale_epoch");
    await waitBounded(leaseLostDone.promise, "lease lost callback");
    await expect(hangingSend).rejects.toThrow();
    expect(sendSignalAborted).toBe(true);

    // Post-loss send and QR calls synchronously throw StaleGatewayEpochError without calling fetch
    await expect(
      wxAdapter.sendTextChunk({
        targetAddress: "weixin:@me",
        recipientPlatformUserId: "wx_alice",
        text: "after lease loss",
        chunkIndex: 0,
        totalChunks: 1,
        weixinPreflight: makePreflightAllow(),
      }),
    ).rejects.toBeInstanceOf(StaleGatewayEpochError);
    await expect(
      wxAdapter.startQrLogin({ userId: USER_ID, sessionKey: "sess_after_loss" }),
    ).rejects.toBeInstanceOf(StaleGatewayEpochError);

    // Release stuck inbound handler -> monitor sees lease lost and MUST NOT advance cursor
    pollBlocked.resolve();
    await wxAdapter.stop();
    expect(backend.updateWeixinAccountCursor).not.toHaveBeenCalled();

    // 2nd gateway instance acquires new epoch and resumes polling from original unadvanced cursor `cursor_v0`
    const resumedCursors: string[] = [];
    const resumedPollSeen = defer<void>();
    vi.mocked(backend.acquireGatewayLease).mockResolvedValueOnce({
      granted: true,
      epoch: 2,
      expires_at: new Date(Date.now() + 45_000).toISOString(),
    });
    const lease2 = new GatewayLeaseManager({
      backend,
      ttlSeconds: 45,
      heartbeatIntervalMs: 60_000,
    });
    await lease2.acquireUntilGranted();
    const wxAdapter2 = new WeixinChannelAdapter({
      backend,
      leaseManager: lease2,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      fetchImpl: async (input, init) => {
        if (String(input).includes("getupdates")) {
          const parsed = JSON.parse(String(init?.body ?? "{}")) as {
            get_updates_buf?: string;
          };
          resumedCursors.push(parsed.get_updates_buf ?? "");
          resumedPollSeen.resolve();
          return await new Promise<Response>((_res, rej) => {
            init?.signal?.addEventListener("abort", () => rej(new Error("aborted")), {
              once: true,
            });
          });
        }
        return makeJsonResponse({ ret: 0 });
      },
    });
    wxAdapter2.onInboundEvent(async () => undefined);
    await wxAdapter2.start();
    await waitBounded(resumedPollSeen.promise, "2nd instance resumed poll");
    expect(resumedCursors).toEqual(["cursor_v0"]);
    await wxAdapter2.stop();
    await lease2.stopAndRelease();
  });

  it("Decision #2: rejects QR and write routes with 503 gateway_starting before lease is acquired without hitting iLink or triggering lease loss; pre-lease 409 in HttpBackendClient does not fire onStaleGatewayEpoch", async () => {
    expect(DEFAULT_GATEWAY_LEASE_ACQUIRE_RETRY_MS).toBe(3_000);

    // Part A: server returns 503 gateway_starting while waiting for lease; /health still returns 200
    const leaseEntered = defer<void>();
    const releaseLease = defer<void>();
    const backend = createMockBackend();
    let leaseLostFired = false;
    let ilinkQrCalls = 0;

    vi.mocked(backend.acquireGatewayLease).mockImplementation(async () => {
      leaseEntered.resolve();
      await releaseLease.promise;
      return {
        granted: true,
        epoch: 11,
        expires_at: new Date(Date.now() + 45_000).toISOString(),
      };
    });

    const wxAdapter = new WeixinChannelAdapter({
      backend,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      fetchImpl: async () => {
        ilinkQrCalls += 1;
        return makeJsonResponse({ ret: 0 });
      },
    });
    const config = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      DALTON_WEIXIN_ENABLED: "true",
    });
    const app = createGatewayApp({
      config,
      backend,
      adapters: new Map<ChannelPlatform, typeof wxAdapter>([["weixin", wxAdapter]]),
      onLeaseLost: () => {
        leaseLostFired = true;
      },
    });

    const startPromise = app.start();
    try {
      await waitBounded(leaseEntered.promise, "lease acquire entered");
      const addr = app.server.address();
      if (!addr || typeof addr === "string") {
        throw new Error("Expected server to be listening on TCP port");
      }
      const base = `http://${addr.address}:${addr.port}`;

      const healthRes = await fetchGatewayServer(app, `${base}/health`, {
        headers: { "X-Service-Token": TEST_SERVICE_TOKEN },
      });
      expect(healthRes.status).toBe(200);

      const qrRes = await fetchGatewayServer(
        app,
        `${base}/internal/weixin/qrcode/start`,
        {
          method: "POST",
          headers: {
            "X-Service-Token": TEST_SERVICE_TOKEN,
            "X-User-Id": USER_ID,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ session_key: "sess_pre_lease" }),
        },
      );
      expect(qrRes.status).toBe(503);
      expect(await qrRes.json()).toMatchObject({ code: "gateway_starting" });

      const sendRes = await fetchGatewayServer(app, `${base}/agent/send`, {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "X-User-Id": USER_ID,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ thread_id: "t1", run_id: "r1", text: "hi" }),
      });
      expect(sendRes.status).toBe(503);
      expect(await sendRes.json()).toMatchObject({ code: "gateway_starting" });

      expect(ilinkQrCalls).toBe(0);
      expect(leaseLostFired).toBe(false);

      releaseLease.resolve();
      await startPromise;
    } finally {
      releaseLease.resolve();
      await app.stop();
    }

    // Part B: HttpBackendClient does NOT fire onStaleGatewayEpoch on 409 before ever holding a lease
    let staleHandlerCalls = 0;
    const httpClient = new HttpBackendClient({
      baseUrl: "http://127.0.0.1:8000",
      serviceToken: TEST_SERVICE_TOKEN,
      fetchImpl: async () =>
        makeJsonResponse(
          { code: "stale_gateway_epoch", message: "missing epoch" },
          409,
        ),
    });
    httpClient.setOnStaleGatewayEpoch(() => {
      staleHandlerCalls += 1;
    });
    await expect(
      httpClient.reconcileWeixinAccounts(null),
    ).rejects.toBeInstanceOf(StaleGatewayEpochError);
    expect(staleHandlerCalls).toBe(0);

    // Once a lease epoch has been held, 409 DOES fire onStaleGatewayEpoch
    httpClient.setGatewayEpoch(5);
    await expect(
      httpClient.reconcileWeixinAccounts(null),
    ).rejects.toBeInstanceOf(StaleGatewayEpochError);
    expect(staleHandlerCalls).toBe(1);
  });

  it("Decision #3: defaults CONTEXT_TOKEN_STALE_ERRCODES to empty and only reports mark_context_token_stale for explicitly configured codes", async () => {
    expect(CONTEXT_TOKEN_STALE_ERRCODES).toEqual([]);

    const backend = createMockBackend();
    const staleTestLease = new GatewayLeaseManager({
      backend,
      ttlSeconds: 45,
      heartbeatIntervalMs: 60_000,
    });
    const staleEpoch = await staleTestLease.acquireUntilGranted();
    backend.setGatewayEpoch(staleEpoch);
    const defaultAdapter = new WeixinChannelAdapter({
      backend,
      leaseManager: staleTestLease,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      fetchImpl: async () =>
        makeJsonResponse({ ret: -2, errcode: 40001, errmsg: "secret" }),
    });
    await defaultAdapter.start();
    try {
      await expect(
        defaultAdapter.sendTextChunk({
          targetAddress: "weixin:@me",
          recipientPlatformUserId: "wx_alice",
          text: "hello",
          chunkIndex: 0,
          totalChunks: 1,
          weixinPreflight: makePreflightAllow({ weixin_context_token_version: 9 }),
        }),
      ).rejects.toMatchObject({ outcome: "failed" });
      expect(backend.updateWeixinAccountStatus).not.toHaveBeenCalled();
    } finally {
      await defaultAdapter.stop();
    }

    // Explicit allowlist triggers mark_context_token_stale with string error_code and expected_token_version
    const explicitAdapter = new WeixinChannelAdapter({
      backend,
      leaseManager: staleTestLease,
      staleContextTokenErrorCodes: [40001],
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      fetchImpl: async () =>
        makeJsonResponse({ ret: -2, errcode: 40001, errmsg: "secret" }),
    });
    await explicitAdapter.start();
    try {
      await expect(
        explicitAdapter.sendTextChunk({
          targetAddress: "weixin:@me",
          recipientPlatformUserId: "wx_alice",
          text: "hello",
          chunkIndex: 0,
          totalChunks: 1,
          weixinPreflight: makePreflightAllow({ weixin_context_token_version: 9 }),
        }),
      ).rejects.toMatchObject({ outcome: "failed" });
      expect(backend.updateWeixinAccountStatus).toHaveBeenCalledWith(
        "wx_acc_1",
        expect.objectContaining({
          action: "mark_context_token_stale",
          error_code: "40001",
          expected_token_version: 9,
        }),
      );
    } finally {
      await explicitAdapter.stop();
      await staleTestLease.stopAndRelease();
    }
  });

  it("Decision #4 & #14: DataTaskNotificationService settles preflight defer as deferred with denyCode, and both DeliveryService and DataTask settle partial+unknown as unknown with file status=unknown", async () => {
    const h = createTestHarness();
    try {
      const pngBytes = Buffer.from("\x89PNG\r\n\x1a\nchart-payload", "binary");
      const pngSha = createHash("sha256").update(pngBytes).digest("hex");
      const runUuid = "11111111-1111-4111-8111-111111111111";

      // 1) DataTask preflight defer -> status: "deferred" and error_code: "roster_lookup_error"
      vi.mocked(h.backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce({
        authorized: true,
        status: "authorized",
        run_id: runUuid,
        owner_user_id: USER_ID,
        owner_platform_user_id: "wx_alice",
        platform: "weixin",
        target_kind: "dm",
        requested_target: "weixin:@me",
        canonical_target_address: "weixin:@me",
        recipient_id: "wx_alice",
        items: [
          {
            item_index: 0,
            delivery_key: "dt_wx_defer_1",
            kind: "text",
            text: "Daily report",
          },
        ],
      });
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
        claim_status: "claimed",
        platform: "weixin",
        target_address: "weixin:@me",
        recipient_id: "wx_alice",
        is_current: false,
        home_workspace_id: HOME_WS_ID,
        weixin_context_token: "ctx_1",
        delivery_attempt: 1,
      });
      vi.mocked(h.backend.preflightWeixinSend).mockResolvedValueOnce({
        authorized: false,
        decision: "defer",
        retryable: true,
        deny_code: "roster_lookup_error",
        message: "Roster DB temporarily unavailable",
      });

      const dtDefer = await h.app.dataTaskNotifications.handleDataTaskNotification({
        run_id: runUuid,
        target: "weixin:@me",
      });
      expect(dtDefer.data.status).toBe("deferred");
      expect(dtDefer.data.code).toBe("roster_lookup_error");
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "dt_wx_defer_1",
        status: "deferred",
        error_code: "roster_lookup_error",
      });

      // 2) DeliveryService: chunk 0 succeeds, chunk 1 throws outcome="unknown" -> settles as "unknown" (NOT "partial")
      let sendCount = 0;
      h.weixinAdapter.setSendTextBehavior(async () => {
        sendCount += 1;
        if (sendCount === 1) {
          return { messageId: "wx_c0" };
        }
        throw new ChannelDeliveryError("Lost response after dispatch", {
          outcome: "unknown",
          code: "unavailable",
        });
      });
      const unknownChunkRes = await h.app.deliveryService.executeDelivery({
        deliveryKey: "del_chunk_partial_unknown",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: `${"A".repeat(1900)}\n${"B".repeat(1900)}`,
      });
      expect(unknownChunkRes.status).toBe("unknown");
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "del_chunk_partial_unknown",
        status: "unknown",
      });

      // 3) DeliveryService: text succeeds, file throws outcome="unknown" -> settles as "unknown" & file status="unknown"
      h.weixinAdapter.setSendTextBehavior(null);
      h.weixinAdapter.setSendFileBehavior(async () => {
        throw new ChannelDeliveryError("File send uncertain", {
          outcome: "unknown",
          code: "unavailable",
        });
      });
      const unknownFileRes = await h.app.deliveryService.executeDelivery({
        deliveryKey: "del_file_partial_unknown",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "Summary text",
        files: [{ path: "notes/report.pdf", workspace_id: HOME_WS_ID }],
      });
      expect(unknownFileRes.status).toBe("unknown");
      expect(unknownFileRes.files).toEqual([
        expect.objectContaining({ path: "notes/report.pdf", status: "unknown" }),
      ]);
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "del_file_partial_unknown",
        status: "unknown",
        files_status: [
          expect.objectContaining({ path: "notes/report.pdf", status: "unknown" }),
        ],
      });

      // 4) DataTaskNotificationService: text succeeds, artifact throws outcome="unknown" -> item & file status="unknown"
      vi.mocked(h.backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce({
        authorized: true,
        status: "authorized",
        run_id: runUuid,
        owner_user_id: USER_ID,
        owner_platform_user_id: "wx_alice",
        platform: "weixin",
        target_kind: "dm",
        requested_target: "weixin:@me",
        canonical_target_address: "weixin:@me",
        recipient_id: "wx_alice",
        items: [
          {
            item_index: 0,
            delivery_key: "dt_wx_file_unknown",
            kind: "text_and_file",
            text: "Chart caption",
            artifact: {
              artifact_id: "art_1",
              filename: "chart.png",
              mime_type: "image/png",
              size_bytes: pngBytes.byteLength,
              sha256: pngSha,
            },
          },
        ],
      });
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
        claim_status: "claimed",
        platform: "weixin",
        target_address: "weixin:@me",
        recipient_id: "wx_alice",
        is_current: false,
        home_workspace_id: HOME_WS_ID,
        weixin_context_token: "ctx_1",
        delivery_attempt: 1,
      });
      vi.mocked(h.backend.downloadDataTaskRunArtifact).mockResolvedValueOnce({
        content: pngBytes,
        mimeType: "image/png",
        filename: "chart.png",
        sha256Header: pngSha,
      });
      const dtUnknownFile = await h.app.dataTaskNotifications.handleDataTaskNotification({
        run_id: runUuid,
        target: "weixin:@me",
      });
      expect(dtUnknownFile.data.status).toBe("unknown");
      expect(dtUnknownFile.data.items[0]?.status).toBe("unknown");
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "dt_wx_file_unknown",
        status: "unknown",
        files_status: [
          expect.objectContaining({ path: "chart.png", status: "unknown" }),
        ],
      });

      // 5) Feishu & Discord regression: definite chunk-2 outcome="failed" after chunk-1 success STILL settles as "partial"
      for (const adapter of [h.feishuAdapter, h.discordAdapter] as const) {
        let count = 0;
        adapter.setSendTextBehavior(async () => {
          count += 1;
          if (count === 1) return { messageId: `${adapter.platform}_m0` };
          throw new ChannelDeliveryError("Definite 400 error", {
            outcome: "failed",
            code: "not_allowed",
          });
        });
        const res = await h.app.deliveryService.executeDelivery({
          deliveryKey: `del_${adapter.platform}_partial_reg`,
          kind: "agent_send",
          userId: USER_ID,
          platform: adapter.platform,
          target: `${adapter.platform}:@me`,
          text: `${"X".repeat(1900)}\n${"Y".repeat(1900)}`,
        });
        expect(res.status).toBe("partial");
      }
    } finally {
      await h.cleanup();
    }
  });

  it("Decision #5, #6, #7, #8: handles missing flushed_inbound text cleanly, treats duplicate collect_buffer_id without collection_mode as collect item, lets /cancel preempt waiting /done, checks backend before upload + passes AbortSignal, and registers recovery rows already uploaded", async () => {
    const h = createTestHarness();
    try {
      // 1) Decision #5: flushed_inbound missing text settles failed without throwing on undefined.trim()
      vi.mocked(h.backend.finalizeWeixinCollectBuffer).mockResolvedValueOnce({
        ok: true,
        status: "done",
        flushed_inbound: {
          platform: "weixin",
          message_id: "collect:buf_missing_text",
          request_key: "req_missing_text",
          dalton_user_id: USER_ID,
          thread_scope_key: "dm:wx_alice",
          workspace_id: HOME_WS_ID,
          external_thread_id: null,
          generation: 1,
          raw_payload: { platform_user_id: "wx_alice" },
          uploaded_attachments: [],
        },
      });
      vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
        mode: "startup",
        inbound_events: [],
        flushing_collect_buffers: [
          {
            buffer_id: "buf_missing_text",
            dalton_user_id: USER_ID,
            weixin_account_id: "wx_acc_1",
            status: "flushing",
          },
        ],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
      });
      await h.app.orchestrator.runStartupRecovery();
      expect(h.backend.createThreadMessages).not.toHaveBeenCalled();
      expect(h.backend.progressUpdates).toContainEqual(
        expect.objectContaining({
          platform: "weixin",
          message_id: "collect:buf_missing_text",
          status: "failed",
        }),
      );

      // 2) Decision #6: duplicate registration with collect_buffer_id but NO collection_mode stays in collect mode
      const uploadStarted = defer<void>();
      const releaseUpload = defer<void>();
      let capturedUploadSignal: AbortSignal | undefined;
      h.weixinAdapter.setAttachment("bot_1:dup_col_1:att:0", {
        content: Buffer.from("file 1", "utf8"),
        filename: "f1.pdf",
        mimeType: "application/pdf",
      });
      vi.mocked(h.backend.uploadWorkspaceFile).mockImplementation(
        async (_u, _ws, _path, _fn, _buf, _mime, signal) => {
          capturedUploadSignal = signal;
          uploadStarted.resolve();
          await new Promise<void>((resolve, reject) => {
            if (signal?.aborted) {
              reject(new Error("aborted"));
              return;
            }
            const onAbort = () => reject(new Error("aborted"));
            signal?.addEventListener("abort", onAbort, { once: true });
            void releaseUpload.promise.then(() => {
              signal?.removeEventListener("abort", onAbort);
              resolve();
            });
          });
        },
      );
      vi.mocked(h.backend.registerAndResolveInbound)
        .mockImplementationOnce(async (req) => ({
          ...emptyInbound(req),
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
          collection_mode: "appended",
          collect_buffer_id: "buf_dup",
          collect_buffer_status: "collecting",
        }))
        .mockImplementationOnce(async (req) => ({
          ...emptyInbound(req),
          existing_status: "pending",
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
          // Omitted collection_mode (duplicate registration)
          collect_buffer_id: "buf_dup",
          collect_buffer_status: "collecting",
        }));

      const colEvent = weixinDm("bot_1:dup_col_1", "doc", {
        attachments: [
          { id: "bot_1:dup_col_1:att:0", filename: "f1.pdf", mimeType: "application/pdf" },
        ],
      });
      await h.weixinAdapter.emitInbound(colEvent);
      await waitBounded(uploadStarted.promise, "workspace upload started");
      // Replay exact same collect message while upload is in-flight -> no duplicate upload, no normal turn
      await h.weixinAdapter.emitInbound(colEvent);
      expect(h.backend.uploadWorkspaceFile).toHaveBeenCalledTimes(1);
      expect(h.backend.createThreadMessages).not.toHaveBeenCalled();

      // 3) Decision #7 & #8: /done transitions buffer to `flushing` (Phase A) then waits on Phase B;
      //    once `flushing` is confirmed, /cancel runs immediately without blocking behind /done,
      //    aborts the in-flight uploadWorkspaceFile AbortSignal, and wakes /done as cancelled.
      const doneEntered = defer<void>();
      const releaseDoneAsCancelled = defer<void>();
      const doneCompleted = defer<void>();
      let bufDupStatus: "collecting" | "flushing" | "cancelled" = "collecting";
      vi.mocked(h.backend.getWeixinCollectBufferStatus).mockImplementation(
        async (bufferId: string) => ({
          ok: true,
          buffer_id: bufferId,
          status: bufferId === "buf_dup" ? bufDupStatus : "collecting",
          upload_status: "pending_upload",
          item_upload_status: "pending_upload",
        }),
      );
      vi.mocked(h.backend.sessionCommand).mockImplementation(async (req) => {
        if (req.cmd === "done") {
          bufDupStatus = "flushing";
          doneEntered.resolve();
          await waitBounded(releaseDoneAsCancelled.promise, "done waiting on cancel");
          doneCompleted.resolve();
          return {
            ok: true,
            linked: true,
            allowed: true,
            dalton_user_id: USER_ID,
            collect_buffer_id: "buf_dup",
            collect_status: "cancelled",
            command_reply_pending: false,
          };
        }
        if (req.cmd === "cancel") {
          bufDupStatus = "cancelled";
          releaseDoneAsCancelled.resolve();
          return {
            ok: true,
            linked: true,
            allowed: true,
            dalton_user_id: USER_ID,
            collect_buffer_id: "buf_dup",
            cancelled_buffer_ids: ["buf_dup"],
            collect_status: "cancelled",
            reply_text: "已取消",
            command_reply_pending: false,
          };
        }
        return { ok: true, linked: true, allowed: true, command_reply_pending: false };
      });

      // emitInbound("/done") confirms `flushing` acceptance and returns promptly without blocking /cancel
      await h.weixinAdapter.emitInbound(weixinDm("bot_1:cmd_done", "/done"));
      await waitBounded(doneEntered.promise, "/done I-3 entered");

      // /cancel executes immediately while /done is still waiting!
      await h.weixinAdapter.emitInbound(weixinDm("bot_1:cmd_cancel", "/cancel"));
      await waitBounded(doneCompleted.promise, "/done resolved as cancelled");
      expect(capturedUploadSignal?.aborted).toBe(true);
      expect(h.backend.createThreadMessages).not.toHaveBeenCalled();
      releaseUpload.resolve();

      // 4) Decision #8: backend reporting cancelled before upload stops attachment #2 before uploadWorkspaceFile
      vi.mocked(h.backend.uploadWorkspaceFile).mockReset();
      vi.mocked(h.backend.uploadWorkspaceFile).mockResolvedValue(undefined);
      vi.mocked(h.backend.getWeixinCollectBufferStatus).mockResolvedValueOnce({
        ok: true,
        buffer_id: "buf_admin_revoked",
        status: "cancelled",
        upload_status: "pending_upload",
        item_upload_status: "pending_upload",
      });
      h.weixinAdapter.setAttachment("bot_1:revoked_item:att:0", {
        content: Buffer.from("should not upload", "utf8"),
        filename: "blocked.pdf",
        mimeType: "application/pdf",
      });
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementationOnce(
        async (req) => ({
          ...emptyInbound(req),
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
          collection_mode: "appended",
          collect_buffer_id: "buf_admin_revoked",
          collect_buffer_status: "collecting",
        }),
      );
      await h.weixinAdapter.emitInbound(
        weixinDm("bot_1:revoked_item", "doc", {
          attachments: [
            {
              id: "bot_1:revoked_item:att:0",
              filename: "blocked.pdf",
              mimeType: "application/pdf",
            },
          ],
        }),
      );
      await new Promise((r) => setTimeout(r, 25));
      expect(h.backend.uploadWorkspaceFile).not.toHaveBeenCalled();

      // 5) Decision #8 (Codex #13): recovery row where alreadyUploaded.length === attachments.length
      //    (uploaded via I-7 before crash, but 补登 not yet called) STILL calls registerWeixinCollectItemAttachments!
      vi.mocked(h.backend.getWeixinCollectBufferStatus).mockResolvedValue({
        ok: true,
        buffer_id: "buf_need_reg",
        status: "collecting",
        upload_status: "pending_upload",
        item_upload_status: "pending_upload",
      });
      vi.mocked(h.backend.registerWeixinCollectItemAttachments).mockClear();
      vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
        mode: "startup",
        inbound_events: [
          {
            platform: "weixin",
            message_id: "bot_1:m_all_uploaded_need_reg",
            platform_user_id: "wx_alice",
            dalton_user_id: USER_ID,
            chat_type: "dm",
            chat_address: "weixin:@me",
            thread_scope_key: "dm:wx_alice",
            request_key: "req_need_reg",
            retry_request_key: null,
            resubmitted_request_key: null,
            steered_into_run_id: null,
            thread_id: null,
            run_id: null,
            turn_index: null,
            workspace_id: HOME_WS_ID,
            external_thread_id: null,
            generation: 1,
            status: "received",
            fallback_pending: false,
            sent_to_current_count: 0,
            uploaded_attachments: [
              {
                filename: "done.pdf",
                workspace_path: "uploads/weixin/done.pdf",
                mime_type: "application/pdf",
                size_bytes: 50,
              },
            ],
            raw_payload: { text: "file" },
            decrypted_weixin_attachments: [
              {
                id: "bot_1:m_all_uploaded_need_reg:att:0",
                kind: "file",
                filename: "done.pdf",
                mimeType: "application/pdf",
                encryptQueryParam: "enc_done",
              },
            ],
            collect_buffer_id: "buf_need_reg",
            collect_buffer_status: "collecting",
            weixin_account_id: "wx_acc_1",
          },
        ],
        flushing_collect_buffers: [],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
      });
      await h.app.orchestrator.runStartupRecovery();
      expect(h.backend.uploadWorkspaceFile).not.toHaveBeenCalled();
      expect(h.backend.registerWeixinCollectItemAttachments).toHaveBeenCalledWith(
        "buf_need_reg",
        "bot_1:m_all_uploaded_need_reg",
        expect.objectContaining({
          uploaded_attachments: [
            expect.objectContaining({ filename: "done.pdf" }),
          ],
        }),
      );
    } finally {
      await h.cleanup();
    }
  });

  it("Decision #9, #10, #12, #13, #15, #16: preflighted command hints without stalling poll, account rotation aborts in-flight send, strict resolveAccountForEvent, monitor self-heal on changed=false & lease recovery, shared 429 cooldown + I-1 503 backoff, QR error redaction, and dual HTTP pools", async () => {
    // 1) Decision #9 / Round 3 Decision #12: Weixin `/bind` & invalid command hints route via I-3 (cmd=help) +
    //    preflight-send (NEVER via I-1 so active collect buffers are not polluted) and swallow errors without stalling
    const h = createTestHarness();
    try {
      await h.weixinAdapter.emitInbound(weixinDm("bot_1:bind_hint_1", "/bind"));
      expect(h.backend.registerAndResolveInbound).not.toHaveBeenCalled();
      expect(h.backend.sessionCommand).toHaveBeenCalledWith(
        expect.objectContaining({
          cmd: "help",
          platform: "weixin",
          message_id: "bot_1:bind_hint_1",
        }),
      );
      expect(h.backend.preflightWeixinSend).toHaveBeenCalled();
      expect(h.weixinAdapter.sentTexts.at(-1)?.text).toContain("扫码绑定");

      // If preflight fails on an invalid command hint, handleInboundEvent does NOT throw (cursor can advance)
      vi.mocked(h.backend.preflightWeixinSend).mockRejectedValueOnce(
        new Error("simulated transient credential error"),
      );
      await expect(
        h.weixinAdapter.emitInbound(weixinDm("bot_1:bad_cmd_1", "/unbind extra")),
      ).resolves.toBeUndefined();
      expect(h.backend.registerAndResolveInbound).not.toHaveBeenCalled();
    } finally {
      await h.cleanup();
    }

    // 2) Decision #10: account rotation aborts in-flight send; strict resolveAccountForEvent rejects mismatched generation
    const backend = createMockBackend();
    const sendStarted = defer<void>();
    let abortedOnRotation = false;
    vi.mocked(backend.reconcileWeixinAccounts)
      .mockResolvedValueOnce({
        changed: true,
        enabled: true,
        max_concurrent_accounts: 80,
        accounts_revision: 1,
        accounts: [
          makeAccountSnapshot({
            account_id: "acc_rot",
            ilink_bot_id: "bot_v1",
            bot_generation: 1,
            bot_token: "tok_v1",
          }),
        ],
      })
      .mockResolvedValueOnce({
        changed: true,
        enabled: true,
        max_concurrent_accounts: 120,
        accounts_revision: 2,
        accounts: [
          makeAccountSnapshot({
            account_id: "acc_rot",
            ilink_bot_id: "bot_v2",
            bot_generation: 2,
            bot_token: "tok_v2",
          }),
        ],
      })
      // Decision #12: changed=false reconcile pass still self-heals a stopped monitor!
      .mockResolvedValueOnce({
        changed: false,
        enabled: true,
        max_concurrent_accounts: 120,
        accounts_revision: 2,
        accounts: [],
      });

    // Decision #16: verify isolated undici Agent pools (poll = maxConcurrentAccounts + 16, control = 64)
    const poolLease = new GatewayLeaseManager({
      backend,
      ttlSeconds: 45,
      heartbeatIntervalMs: 60_000,
    });
    const poolEpoch = await poolLease.acquireUntilGranted();
    backend.setGatewayEpoch(poolEpoch);
    const createdPools: Array<{
      connections: number;
      id: number;
      close: () => void;
      destroy: () => void;
    }> = [];
    const seenDispatchersByEndpoint = new Map<string, unknown[]>();
    const poolAdapter = new WeixinChannelAdapter({
      backend,
      leaseManager: poolLease,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      httpAgentFactory: (opts) => {
        const pool = {
          connections: opts.connections,
          id: createdPools.length + 1,
          close: () => undefined,
          destroy: () => undefined,
        };
        createdPools.push(pool);
        return pool;
      },
      fetchImpl: async (input, init) => {
        const url = String(input);
        const disp = (init as RequestInit & { dispatcher?: unknown })?.dispatcher;
        const ep = url.includes("getupdates")
          ? "getupdates"
          : url.includes("sendmessage")
            ? "sendmessage"
            : "other";
        const arr = seenDispatchersByEndpoint.get(ep) ?? [];
        arr.push(disp);
        seenDispatchersByEndpoint.set(ep, arr);

        if (url.includes("sendmessage")) {
          sendStarted.resolve();
          return await new Promise<Response>((_res, rej) => {
            init?.signal?.addEventListener(
              "abort",
              () => {
                abortedOnRotation = true;
                rej(new Error("aborted"));
              },
              { once: true },
            );
          });
        }
        return await new Promise<Response>((_res, rej) => {
          init?.signal?.addEventListener("abort", () => rej(new Error("aborted")), {
            once: true,
          });
        });
      },
    });
    poolAdapter.onInboundEvent(async () => undefined);
    await poolAdapter.start();
    try {
      // Poll pool resized to 80 + 16 = 96; control pool = 64
      expect(poolAdapter.getHttpPoolManager().getPollPoolConnections()).toBe(96);
      expect(poolAdapter.getHttpPoolManager().getControlPoolConnections()).toBe(64);

      const inFlightSend = poolAdapter.sendTextChunk({
        targetAddress: "weixin:@me",
        recipientPlatformUserId: "wx_alice",
        text: "before rotation",
        chunkIndex: 0,
        totalChunks: 1,
        weixinPreflight: makePreflightAllow({
          account_id: "acc_rot",
          ilink_bot_id: "bot_v1",
          bot_generation: 1,
          bot_token: "tok_v1",
        }),
      });
      await waitBounded(sendStarted.promise, "sendmessage before rotation");

      // Reconcile rotates acc_rot from gen 1 -> gen 2 and resizes max_concurrent_accounts to 120 (pool=136)
      await poolAdapter.reconcileNow("rotation");
      await expect(inFlightSend).rejects.toThrow();
      expect(abortedOnRotation).toBe(true);
      expect(poolAdapter.getHttpPoolManager().getPollPoolConnections()).toBe(136);
      expect(seenDispatchersByEndpoint.get("getupdates")?.[0]).not.toBe(
        seenDispatchersByEndpoint.get("sendmessage")?.[0],
      );

      // Strict resolveAccountForEvent: stale gen 1 event MUST NOT receive typing feedback using gen 2 credentials
      const staleHandle = poolAdapter.beginProcessingFeedback({
        event: weixinDm("bot_v1:stale_msg", "hi", {
          weixinAccountId: "acc_rot",
          weixinBotId: "bot_v1",
          weixinBotGeneration: 1,
        }),
      });
      expect(staleHandle).toBeNull();
    } finally {
      await poolAdapter.stop();
      await poolLease.stopAndRelease();
    }

    // 3) Decision #12: self-heal on changed=false AND on lease recovery
    const healBackend = createMockBackend();
    let pollCallsAfterHeal = 0;
    const healedPollSeen = defer<void>();
    let leaseLocallyValid = true;
    vi.mocked(healBackend.acquireGatewayLease).mockResolvedValue({
      granted: true,
      epoch: 3,
      expires_at: new Date(Date.now() + 45_000).toISOString(),
    });
    vi.mocked(healBackend.heartbeatGatewayLease).mockResolvedValue({
      ok: true,
      epoch: 3,
      expires_at: new Date(Date.now() + 45_000).toISOString(),
    });
    vi.mocked(healBackend.reconcileWeixinAccounts)
      .mockResolvedValueOnce({
        changed: true,
        enabled: true,
        max_concurrent_accounts: 50,
        accounts_revision: 10,
        accounts: [makeAccountSnapshot({ account_id: "acc_heal" })],
      })
      .mockResolvedValue({
        changed: false,
        enabled: true,
        max_concurrent_accounts: 50,
        accounts_revision: 10,
        accounts: [],
      });

    const healLease = new GatewayLeaseManager({
      backend: healBackend,
      ttlSeconds: 45,
      heartbeatIntervalMs: 60_000,
    });
    await healLease.acquireUntilGranted();
    const origIsValid = healLease.isLeaseLocallyValid.bind(healLease);
    vi.spyOn(healLease, "isLeaseLocallyValid").mockImplementation(
      () => leaseLocallyValid && origIsValid(),
    );

    const healAdapter = new WeixinChannelAdapter({
      backend: healBackend,
      leaseManager: healLease,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      fetchImpl: async (_input, init) => {
        pollCallsAfterHeal += 1;
        if (pollCallsAfterHeal === 1) {
          // Simulate transient local lease dip during 1st poll -> monitor coroutine exits
          leaseLocallyValid = false;
          return makeJsonResponse({ ret: 0, msgs: [] });
        }
        healedPollSeen.resolve();
        return await new Promise<Response>((_res, rej) => {
          init?.signal?.addEventListener("abort", () => rej(new Error("aborted")), {
            once: true,
          });
        });
      },
    });
    healAdapter.onInboundEvent(async () => undefined);
    await healAdapter.start();
    try {
      for (let i = 0; i < 20 && healAdapter.getManagedAccounts()[0]?.polling; i++) {
        await new Promise((r) => setTimeout(r, 10));
      }
      expect(healAdapter.getManagedAccounts()[0]?.polling).toBe(false);

      // Lease becomes valid again; changed=false reconcile self-heals the exited monitor!
      leaseLocallyValid = true;
      await healAdapter.reconcileNow("self_heal_check");
      await waitBounded(healedPollSeen.promise, "self-healed monitor resumed polling");
      expect(healAdapter.getManagedAccounts()[0]?.polling).toBe(true);
    } finally {
      await healAdapter.stop();
      await healLease.stopAndRelease();
    }

    // 4) Decision #13: shared 429 cooldown gates ALL accounts; I-1 503 backs off exponentially
    const rlBackend = createMockBackend();
    let accAPolls = 0;
    let accBPolls = 0;
    const accBFirstPoll = defer<void>();
    vi.mocked(rlBackend.reconcileWeixinAccounts).mockResolvedValue({
      changed: true,
      enabled: true,
      max_concurrent_accounts: 50,
      accounts_revision: 1,
      accounts: [
        makeAccountSnapshot({ account_id: "acc_429_a", ilink_bot_id: "bot_a", bot_token: "tok_a" }),
        makeAccountSnapshot({ account_id: "acc_429_b", ilink_bot_id: "bot_b", bot_token: "tok_b" }),
      ],
    });
    const rlAdapter = new WeixinChannelAdapter({
      backend: rlBackend,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      rateLimitAccountThreshold: 1,
      rateLimitCooldownMs: 250,
      fetchImpl: async (_input, init) => {
        const auth = headerValue(init, "authorization");
        if (auth === "Bearer tok_a") {
          accAPolls += 1;
          return makeJsonResponse({ error: "rate limit" }, 429, { "retry-after": "1" });
        }
        accBPolls += 1;
        accBFirstPoll.resolve();
        return makeJsonResponse({ ret: 0, msgs: [] });
      },
    });
    rlAdapter.onInboundEvent(async () => undefined);
    await rlAdapter.start();
    try {
      await waitBounded(accBFirstPoll.promise, "acc_b first poll");
      await new Promise((r) => setTimeout(r, 80));
      // Because acc_a triggered shared 250ms cooldown, acc_b MUST NOT spin rapidly during cooldown
      expect(accAPolls).toBe(1);
      expect(accBPolls).toBeLessThanOrEqual(2);
    } finally {
      await rlAdapter.stop();
    }

    // I-1 503 exponential backoff: 3 consecutive 503s wait >= 10ms + 20ms (not reset to 10ms)
    const backoffBackend = createMockBackend();
    const pollTimestamps: number[] = [];
    const threePollsDone = defer<void>();
    vi.mocked(backoffBackend.reconcileWeixinAccounts).mockResolvedValue({
      changed: true,
      enabled: true,
      max_concurrent_accounts: 50,
      accounts_revision: 1,
      accounts: [makeAccountSnapshot({ account_id: "acc_backoff" })],
    });
    const backoffAdapter = new WeixinChannelAdapter({
      backend: backoffBackend,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      retryDelayMs: 25,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      fetchImpl: async () => {
        pollTimestamps.push(Date.now());
        if (pollTimestamps.length >= 3) {
          threePollsDone.resolve();
        }
        return makeJsonResponse({
          ret: 0,
          get_updates_buf: "buf_retry",
          msgs: [
            {
              message_id: "8888",
              from_user_id: "wx_alice",
              item_list: [{ type: 1, text_item: { text: "retry me" } }],
            },
          ],
        });
      },
    });
    backoffAdapter.onInboundEvent(async () => {
      throw new BackendHttpError("roster unavailable", 503, { code: "roster_lookup_error" });
    });
    await backoffAdapter.start();
    try {
      await waitBounded(threePollsDone.promise, "3 I-1 503 backoff polls");
      const gap1 = pollTimestamps[1]! - pollTimestamps[0]!;
      const gap2 = pollTimestamps[2]! - pollTimestamps[1]!;
      // 2nd backoff (~50ms) is strictly greater than 1st backoff (~25ms)
      expect(gap2).toBeGreaterThan(gap1);
    } finally {
      await backoffAdapter.stop();
    }

    // 5) Decision #15: QR HTTP error NEVER leaks qrcode ticket in error message or endpoint
    try {
      await pollWeixinQrStatus({
        baseUrl: "https://ilinkai.weixin.qq.com",
        qrcode: "SECRET_QR_TICKET_99999",
        allowedDomains: ["ilinkai.weixin.qq.com"],
        checkResolvedIps: false,
        fetchImpl: async () => new Response("internal backend leak", { status: 502 }),
      });
      expect.unreachable("Expected pollWeixinQrStatus HTTP 502 to reject");
    } catch (err) {
      expect(err).toBeInstanceOf(WeixinApiError);
      const apiErr = err as WeixinApiError;
      expect(apiErr.endpoint).toBe("ilink/bot/get_qrcode_status");
      expect(apiErr.message).not.toContain("SECRET_QR_TICKET_99999");
      expect(apiErr.message).not.toContain("internal backend leak");
    }

    // Verify HTTP pool manager unit helper
    const poolMgr = new WeixinHttpPoolManager({
      maxConcurrentAccounts: 200,
      agentFactory: (opts) => ({
        close: () => undefined,
        ...opts,
      }),
    });
    expect(poolMgr.getPollPoolConnections()).toBe(216);
    expect(poolMgr.getControlPoolConnections()).toBe(64);
    expect(poolMgr.getPollDispatcher()).not.toBe(poolMgr.getControlDispatcher());
    await poolMgr.close();
  });
});

// ===========================================================================
// Batch B Round 3 — Contract Alignment & Review Fixes (Decisions #1 ~ #13)
// ===========================================================================

describe("Batch B Round 3 — Lease Timer, Preflight Snapshot, Collect, /cancel, /done, Partial Deferred, Real Undici & Regressions", () => {
  it("Decision #1: independent expires_at timer aborts hanging sendmessage even when heartbeat hangs; <5s safety margin pauses without exiting; DNS-phase expiry blocks HTTP dispatch", async () => {
    const backend = createMockBackend();
    const leaseLostReasons: string[] = [];
    const sendStarted = defer<void>();
    let sendSignalAborted = false;

    // 1) Independent expiry timer fires at expires_at even while heartbeat hangs indefinitely
    vi.mocked(backend.acquireGatewayLease).mockResolvedValueOnce({
      granted: true,
      epoch: 11,
      expires_at: new Date(Date.now() + 90).toISOString(),
    });
    vi.mocked(backend.heartbeatGatewayLease).mockImplementation(
      () => new Promise(() => undefined), // hangs forever
    );

    const adapter = new WeixinChannelAdapter({
      backend,
      accountId: "acc_exp",
      ilinkBotId: "bot_exp",
      botToken: "tok_exp",
      botGeneration: 1,
      checkResolvedIps: false,
      fetchImpl: async (_url, init) => {
        sendStarted.resolve();
        await new Promise<void>((_res, rej) => {
          init?.signal?.addEventListener(
            "abort",
            () => {
              sendSignalAborted = true;
              rej(new Error("aborted by expiry timer"));
            },
            { once: true },
          );
        });
        return new Response("{}", { status: 200 });
      },
    });
    const lease = new GatewayLeaseManager({
      backend,
      ttlSeconds: 1,
      heartbeatIntervalMs: 30,
      safetyMarginMs: 10,
      onLeaseLost: (reason) => {
        leaseLostReasons.push(reason);
        adapter.abortAllInFlight();
      },
    });
    adapter.setLeaseManager(lease);
    await lease.acquireUntilGranted();

    const sendPromise = adapter
      .sendTextChunk({
        targetAddress: "weixin:@me",
        recipientPlatformUserId: "wx_alice",
        text: "hello",
        chunkIndex: 0,
        totalChunks: 1,
        weixinContextToken: "ctx_1",
      })
      .catch((e) => e);

    await waitBounded(sendStarted.promise, "sendmessage started");
    const sendErr = await waitBounded(sendPromise, "send aborted by expires_at timer");
    expect(sendSignalAborted).toBe(true);
    expect(leaseLostReasons).toContain("lease_expired_timer");
    expect(sendErr).toBeInstanceOf(Error);
    await adapter.stop();

    // 2) <5s safety margin ONLY pauses new requests (deferred, NOT fatal StaleGatewayEpochError / exit)
    const marginBackend = createMockBackend();
    const marginLost: string[] = [];
    vi.mocked(marginBackend.acquireGatewayLease).mockResolvedValueOnce({
      granted: true,
      epoch: 12,
      // 2.5s remaining -> inside default 5s safety margin, but NOT expired
      expires_at: new Date(Date.now() + 2_500).toISOString(),
    });
    const marginLease = new GatewayLeaseManager({
      backend: marginBackend,
      ttlSeconds: 45,
      heartbeatIntervalMs: 60_000,
      safetyMarginMs: 5_000,
      onLeaseLost: (reason) => {
        marginLost.push(reason);
      },
    });
    await marginLease.acquireUntilGranted();
    expect(marginLease.isLeaseExpired()).toBe(false);
    expect(marginLease.isInSafetyMargin()).toBe(true);
    expect(() => marginLease.assertLeaseForOutbound("sendTextChunk")).toThrow(
      LeaseSafetyMarginPauseError,
    );
    expect(marginLost).toEqual([]);

    const marginAdapter = new WeixinChannelAdapter({
      backend: marginBackend,
      leaseManager: marginLease,
      accountId: "acc_m",
      ilinkBotId: "bot_m",
      botToken: "tok_m",
      checkResolvedIps: false,
    });
    await expect(
      marginAdapter.sendTextChunk({
        targetAddress: "weixin:@me",
        recipientPlatformUserId: "wx_alice",
        text: "paused during margin",
        chunkIndex: 0,
        totalChunks: 1,
        weixinContextToken: "ctx_m",
      }),
    ).rejects.toMatchObject({
      outcome: "deferred",
      retryable: true,
      denyCode: "gateway_lease_safety_margin",
    });
    expect(marginLost).toEqual([]);
    await marginLease.stopAndRelease();
    await marginAdapter.stop();

    // 3) Lease expiring DURING DNS resolution stops send BEFORE fetchImpl is ever invoked
    let fetchCalledAfterDns = false;
    let leaseValid = true;
    await expect(
      sendWeixinTextMessage(
        {
          baseUrl: "https://ilinkai.weixin.qq.com",
          cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          botToken: "tok_dns",
          contextToken: "ctx_dns",
          toUserId: "wx_alice",
          checkResolvedIps: true,
          resolveHostIps: async () => {
            leaseValid = false; // simulate lease expiry while DNS resolves
            return ["1.1.1.1"];
          },
          assertLeaseValid: () => {
            if (!leaseValid) {
              throw new StaleGatewayEpochError("Lease expired during DNS", {
                status: 409,
                epoch: 12,
              });
            }
          },
          fetchImpl: async () => {
            fetchCalledAfterDns = true;
            return new Response(JSON.stringify({ ret: 0 }), { status: 200 });
          },
        },
        "must not dispatch",
      ),
    ).rejects.toBeInstanceOf(StaleGatewayEpochError);
    expect(fetchCalledAfterDns).toBe(false);
  });

  it("Decision #2 & #13: creates abort signal BEFORE preflight-send & getWeixinAccountDetail, and rejects preflight bot_generation mismatch against managed snapshot", async () => {
    const h = createTestHarness();
    let ilinkCalled = 0;
    vi.mocked(h.backend.reconcileWeixinAccounts).mockResolvedValue({
      changed: true,
      enabled: true,
      max_concurrent_accounts: 50,
      accounts_revision: 2,
      accounts: [
        makeAccountSnapshot({
          account_id: "wx_acc_1",
          ilink_bot_id: "bot_1",
          bot_generation: 2,
          bot_token: "tok_1",
        }),
      ],
    });
    const realAdapter = new WeixinChannelAdapter({
      backend: h.backend,
      accountId: "wx_acc_1",
      ilinkBotId: "bot_1",
      botToken: "tok_1",
      botGeneration: 2,
      checkResolvedIps: false,
      fetchImpl: async (url, init) => {
        const urlStr = String(url);
        if (urlStr.includes("getupdates")) {
          await new Promise<void>((_res, rej) => {
            init?.signal?.addEventListener("abort", () => rej(new Error("aborted")), {
              once: true,
            });
          });
        }
        ilinkCalled += 1;
        return new Response(JSON.stringify({ ret: 0, message_id: "m_1" }), {
          status: 200,
        });
      },
    });
    h.adapters.set("weixin", realAdapter as unknown as typeof h.weixinAdapter);

    try {
      // 1) Preflight returns stale bot_generation=1 while managed snapshot is bot_generation=2 -> refused before iLink
      vi.mocked(h.backend.preflightWeixinSend).mockResolvedValueOnce({
        authorized: true,
        decision: "allow",
        retryable: false,
        chunk_index: 0,
        account_id: "wx_acc_1",
        ilink_bot_id: "bot_1",
        bot_generation: 1,
        bot_token: "tok_old",
        base_url: "https://ilinkai.weixin.qq.com",
        cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
        recipient_id: "wx_alice",
        weixin_context_token: "ctx_old",
        weixin_context_token_version: 1,
      });

      const ds = new (await import("../src/delivery-service.js")).DeliveryService({
        backend: h.backend,
        adapters: h.adapters,
      });
      // Mark realAdapter started/connected for DeliveryService check
      await realAdapter.start();
      const mismatchRes = await ds.executeDelivery({
        deliveryKey: "del_snap_mismatch",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "stale gen must not send",
      });
      expect(mismatchRes.status).toBe("failed");
      expect(ilinkCalled).toBe(0);
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "del_snap_mismatch",
        status: "failed",
        error_code: "weixin_bot_generation_mismatch",
      });

      // 2) Abort triggered WHILE preflightWeixinSend is in flight cancels before sendmessage
      const preflightEntered = defer<void>();
      const releasePreflight = defer<void>();
      vi.mocked(h.backend.preflightWeixinSend).mockImplementationOnce(async () => {
        preflightEntered.resolve();
        await waitBounded(releasePreflight.promise, "release preflight");
        return {
          authorized: true,
          decision: "allow",
          retryable: false,
          chunk_index: 0,
          account_id: "wx_acc_1",
          ilink_bot_id: "bot_1",
          bot_generation: 2,
          bot_token: "tok_1",
          base_url: "https://ilinkai.weixin.qq.com",
          cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
          recipient_id: "wx_alice",
          weixin_context_token: "ctx_2",
          weixin_context_token_version: 2,
        };
      });
      const deliveryPromise = ds.executeDelivery({
        deliveryKey: "del_abort_during_preflight",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "abort during preflight",
      });
      await waitBounded(preflightEntered.promise, "preflight entered");
      realAdapter.abortAllInFlight();
      releasePreflight.resolve();
      const abortedRes = await deliveryPromise;
      expect(abortedRes.status).toBe("failed");
      expect(ilinkCalled).toBe(0);

      // 3) Decision #13: beginProcessingFeedback creates abort signal BEFORE getWeixinAccountDetail,
      //    so abortAllInFlight during getWeixinAccountDetail prevents getconfig from ever running.
      const detailStarted = defer<void>();
      const releaseDetail = defer<void>();
      vi.mocked(h.backend.getWeixinAccountDetail).mockImplementationOnce(
        async () => {
          detailStarted.resolve();
          await waitBounded(releaseDetail.promise, "release detail");
          return {
            account_id: "wx_acc_1",
            user_id: USER_ID,
            ilink_bot_id: "bot_1",
            ilink_user_id: "wx_alice",
            bot_token: "tok_1",
            base_url: "https://ilinkai.weixin.qq.com",
            cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
            bot_generation: 2,
            status: "active",
            get_updates_buf: "",
            weixin_context_token: "ctx_typing",
          };
        },
      );
      const handle = realAdapter.beginProcessingFeedback({
        event: weixinDm("bot_1:typing_abort_1", "hello", {
          weixinAccountId: "wx_acc_1",
          weixinBotId: "bot_1",
          weixinBotGeneration: 2,
          weixinContextToken: undefined,
        }),
      });
      expect(handle).not.toBeNull();
      await waitBounded(detailStarted.promise, "getWeixinAccountDetail entered");
      realAdapter.abortAllInFlight();
      releaseDetail.resolve();
      await handle!.settle("done");
      expect(ilinkCalled).toBe(0);
    } finally {
      await realAdapter.stop();
      await h.cleanup();
    }
  });

  it("Decision #3, #4, #5, #7: collect status 5xx blocks upload, too_late cleans up file, /cancel cancels only cancelled_buffer_ids, /done 503 retains cursor & notifies user, normal upload takes AbortSignal", async () => {
    const h = createTestHarness();
    try {
      // 1) Decision #3: status query 500 blocks upload (never defaults to pending, never marks upload_failed)
      vi.mocked(h.backend.getWeixinCollectBufferStatus).mockRejectedValueOnce(
        new BackendHttpError("500 internal error", 500, {}),
      );
      h.weixinAdapter.setAttachment("att_500", {
        content: Buffer.from("pdf", "utf8"),
        filename: "doc500.pdf",
        mimeType: "application/pdf",
      });
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementationOnce(
        async (req) => ({
          ...emptyInbound(req),
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
          collection_mode: "appended",
          collect_buffer_id: "buf_500",
          collect_buffer_status: "collecting",
        }),
      );
      await h.weixinAdapter.emitInbound(
        weixinDm("bot_1:m_500", "doc", {
          attachments: [
            { id: "att_500", filename: "doc500.pdf", mimeType: "application/pdf" },
          ],
        }),
      );
      await new Promise((r) => setTimeout(r, 25));
      expect(h.backend.uploadWorkspaceFile).not.toHaveBeenCalled();
      expect(h.backend.registerWeixinCollectItemAttachments).not.toHaveBeenCalled();

      // 2) Decision #3 & #7: registerWeixinCollectItemAttachments returning `too_late` deletes uploaded file cleanly
      vi.mocked(h.backend.getWeixinCollectBufferStatus).mockResolvedValue({
        ok: true,
        buffer_id: "buf_late",
        status: "flushing",
        upload_status: "pending_upload",
        item_upload_status: "pending_upload",
      });
      vi.mocked(h.backend.registerWeixinCollectItemAttachments).mockResolvedValueOnce({
        ok: true,
        upload_status: "too_late",
        collect_buffer_status: "done",
        code: "collect_buffer_too_late",
      });
      h.weixinAdapter.setAttachment("att_late", {
        content: Buffer.from("late-pdf", "utf8"),
        filename: "late.pdf",
        mimeType: "application/pdf",
      });
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementationOnce(
        async (req) => ({
          ...emptyInbound(req),
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
          collection_mode: "appended",
          collect_buffer_id: "buf_late",
          collect_buffer_status: "flushing",
        }),
      );
      await h.weixinAdapter.emitInbound(
        weixinDm("bot_1:m_late", "late", {
          attachments: [
            { id: "att_late", filename: "late.pdf", mimeType: "application/pdf" },
          ],
        }),
      );
      await new Promise((r) => setTimeout(r, 30));
      expect(h.backend.deleteWorkspaceFiles).toHaveBeenCalledWith(
        USER_ID,
        HOME_WS_ID,
        [expect.stringContaining("late.pdf")],
      );

      // 3) Decision #4 & #7: `/cancel` cancels ONLY `cancelled_buffer_ids` returned by I-3 (not newer buffer on same account),
      //    and cleans up cancelled buffer's uploaded workspace file.
      vi.mocked(h.backend.deleteWorkspaceFiles).mockClear();
      vi.mocked(h.backend.getWeixinCollectBufferStatus).mockImplementation(
        async (bufferId: string) => ({
          ok: true,
          buffer_id: bufferId,
          status: "collecting",
          upload_status: "pending_upload",
          item_upload_status: "pending_upload",
        }),
      );
      const oldUploadStarted = defer<void>();
      const newUploadStarted = defer<void>();
      const releaseUploads = defer<void>();
      const signalsByFile = new Map<string, AbortSignal | undefined>();
      vi.mocked(h.backend.uploadWorkspaceFile).mockImplementation(
        async (_u, _ws, _path, filename, _c, _m, signal) => {
          signalsByFile.set(filename, signal);
          if (filename === "old.pdf") oldUploadStarted.resolve();
          if (filename === "new.pdf") newUploadStarted.resolve();
          await new Promise<void>((resolve, reject) => {
            signal?.addEventListener("abort", () => reject(new Error("aborted")), {
              once: true,
            });
            void releaseUploads.promise.then(resolve);
          });
        },
      );
      h.weixinAdapter.setAttachment("att_old", {
        content: Buffer.from("old", "utf8"),
        filename: "old.pdf",
        mimeType: "application/pdf",
      });
      h.weixinAdapter.setAttachment("att_new", {
        content: Buffer.from("new", "utf8"),
        filename: "new.pdf",
        mimeType: "application/pdf",
      });
      vi.mocked(h.backend.registerAndResolveInbound)
        .mockImplementationOnce(async (req) => ({
          ...emptyInbound(req),
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
          collection_mode: "appended",
          collect_buffer_id: "buf_old",
          collect_buffer_status: "collecting",
        }))
        .mockImplementationOnce(async (req) => ({
          ...emptyInbound(req),
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
          collection_mode: "appended",
          collect_buffer_id: "buf_new",
          collect_buffer_status: "collecting",
        }));

      await h.weixinAdapter.emitInbound(
        weixinDm("bot_1:m_old", "old", {
          attachments: [{ id: "att_old", filename: "old.pdf", mimeType: "application/pdf" }],
        }),
      );
      await h.weixinAdapter.emitInbound(
        weixinDm("bot_1:m_new", "new", {
          attachments: [{ id: "att_new", filename: "new.pdf", mimeType: "application/pdf" }],
        }),
      );
      await waitBounded(oldUploadStarted.promise, "old upload started");
      await waitBounded(newUploadStarted.promise, "new upload started");

      vi.mocked(h.backend.sessionCommand).mockResolvedValueOnce({
        ok: true,
        linked: true,
        allowed: true,
        dalton_user_id: USER_ID,
        collect_status: "cancelled",
        cancelled_buffer_ids: ["buf_old"],
        command_reply_pending: false,
      });
      await h.weixinAdapter.emitInbound(weixinDm("bot_1:m_cancel_sel", "/cancel"));
      await new Promise((r) => setTimeout(r, 25));

      expect(signalsByFile.get("old.pdf")?.aborted).toBe(true);
      expect(signalsByFile.get("new.pdf")?.aborted).toBe(false);
      expect(h.backend.deleteWorkspaceFiles).toHaveBeenCalledWith(
        USER_ID,
        HOME_WS_ID,
        [expect.stringContaining("old.pdf")],
      );
      releaseUploads.resolve();
      await new Promise((r) => setTimeout(r, 25));

      // 4) Decision #5: `/done` acceptance failure (503) rejects so monitor does NOT advance cursor,
      //    and sends a preflighted failure notice to the user.
      vi.mocked(h.backend.sessionCommand).mockRejectedValueOnce(
        new BackendHttpError("503 roster lookup error", 503, {
          code: "roster_lookup_error",
        }),
      );
      vi.mocked(h.backend.preflightWeixinSend).mockClear();
      await expect(
        h.app.orchestrator.handleInboundEvent(
          weixinDm("bot_1:m_done_503", "/done"),
        ),
      ).rejects.toBeInstanceOf(BackendHttpError);
      expect(h.backend.preflightWeixinSend).toHaveBeenCalled();

      // 5) Decision #7: normal inbound attachment upload receives an AbortSignal that aborts on lease loss
      let normalUploadSignal: AbortSignal | undefined;
      const normalUploadStarted = defer<void>();
      vi.mocked(h.backend.uploadWorkspaceFile).mockImplementationOnce(
        async (_u, _ws, _path, _fn, _c, _m, signal) => {
          normalUploadSignal = signal;
          normalUploadStarted.resolve();
          await new Promise<void>((_res, rej) => {
            signal?.addEventListener("abort", () => rej(new Error("aborted")), {
              once: true,
            });
          });
        },
      );
      h.weixinAdapter.setAttachment("att_norm", {
        content: Buffer.from("normal", "utf8"),
        filename: "norm.pdf",
        mimeType: "application/pdf",
      });
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementationOnce(
        async (req) => ({
          ...emptyInbound(req),
          linked: true,
          allowed: true,
          dalton_user_id: USER_ID,
          workspace_id: HOME_WS_ID,
          workspace_status: "running",
        }),
      );
      await h.weixinAdapter.emitInbound(
        weixinDm("bot_1:m_norm_upload", "check file", {
          attachments: [
            { id: "att_norm", filename: "norm.pdf", mimeType: "application/pdf" },
          ],
        }),
      );
      await waitBounded(normalUploadStarted.promise, "normal upload started");
      expect(normalUploadSignal).toBeDefined();
      expect(normalUploadSignal?.aborted).toBe(false);
      h.app.orchestrator.abortAllActiveCollectUploads();
      expect(normalUploadSignal?.aborted).toBe(true);
    } finally {
      await h.cleanup();
    }
  });

  it("Decision #6: handles I-6 claim_status='deferred', settles chunk-2 / file defer as partial_deferred with breakpoints, and resumes remaining segments on retry (DeliveryService & DataTask)", async () => {
    const h = createTestHarness();
    try {
      // 1) DeliveryService: I-6 claim_status="deferred" -> returns deferred without sending or calling I-7 failed
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
        claim_status: "deferred",
        status: "partial_deferred",
        code: "delivery_deferred",
        retryable: true,
        defer_count: 1,
        retry_after_ms: 10_000,
        platform: "weixin",
        target_address: "weixin:@me",
        sent_chunks: 1,
        total_chunks: 2,
        platform_message_ids: ["wx_c0"],
        delivered_segments: [{ kind: "text", index: 0, message_id: "wx_c0" }],
        is_current: true,
      });
      const deferredClaimRes = await h.app.deliveryService.executeDelivery({
        deliveryKey: "del_not_due_yet",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: "Chunk 1\nChunk 2",
      });
      expect(deferredClaimRes.status).toBe("partial_deferred");
      expect(h.weixinAdapter.sentTexts).toHaveLength(0);
      expect(h.backend.deliveryUpdates).toHaveLength(0);

      // 2) DeliveryService: chunk 0 succeeds, chunk 1 hits preflight defer -> settles `partial_deferred` with breakpoints
      vi.mocked(h.backend.preflightWeixinSend)
        .mockResolvedValueOnce({
          authorized: true,
          decision: "allow",
          retryable: false,
          chunk_index: 0,
          account_id: "wx_acc_1",
          ilink_bot_id: "bot_1",
          bot_generation: 1,
          bot_token: "tok_1",
          base_url: "https://ilinkai.weixin.qq.com",
          cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
          recipient_id: "wx_alice",
          weixin_context_token: "ctx_1",
          weixin_context_token_version: 1,
        })
        .mockResolvedValueOnce({
          authorized: false,
          decision: "defer",
          retryable: true,
          deny_code: "roster_lookup_error",
          message: "Roster temporarily unavailable",
        });

      const twoChunkText = `${"A".repeat(1900)}\n${"B".repeat(1900)}`;
      const partialDefRes = await h.app.deliveryService.executeDelivery({
        deliveryKey: "del_partial_defer_1",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: twoChunkText,
      });
      expect(partialDefRes.status).toBe("partial_deferred");
      expect(h.weixinAdapter.sentTexts).toHaveLength(1);
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "del_partial_defer_1",
        status: "partial_deferred",
        outcome: "partial_deferred",
        sent_chunks: 1,
        total_chunks: 2,
        error_code: "roster_lookup_error",
        delivered_segments: [expect.objectContaining({ kind: "text", index: 0 })],
      });

      // 3) Retry DeliveryService with breakpoint `sent_chunks: 1` -> skips chunk 0, sends ONLY chunk 1, settles `sent`
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
        claim_status: "claimed",
        platform: "weixin",
        target_address: "weixin:@me",
        recipient_id: "wx_alice",
        is_current: true,
        delivery_attempt: 2,
        sent_chunks: 1,
        total_chunks: 2,
        platform_message_ids: [h.weixinAdapter.sentTexts[0]!.messageId],
        files_status: [],
        delivered_segments: [
          {
            kind: "text",
            index: 0,
            message_id: h.weixinAdapter.sentTexts[0]!.messageId,
          },
        ],
        weixin_account_id: "wx_acc_1",
        weixin_bot_id: "bot_1",
        weixin_bot_generation: 1,
        weixin_context_token: "ctx_1",
      });
      const resumedRes = await h.app.deliveryService.executeDelivery({
        deliveryKey: "del_partial_defer_1",
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        text: twoChunkText,
      });
      expect(resumedRes.status).toBe("sent");
      expect(h.weixinAdapter.sentTexts).toHaveLength(2);
      expect(h.weixinAdapter.sentTexts[1]!.text).toContain("B".repeat(100));

      // 4) DataTaskNotificationService: text succeeds, file defers -> `partial_deferred`;
      //    retry with `sent_chunks: 1` skips text and sends ONLY the file!
      const pngBytes = Buffer.from("\x89PNG\r\n\x1a\npartial-defer-chart", "binary");
      const pngSha = createHash("sha256").update(pngBytes).digest("hex");
      const runUuid = "22222222-2222-4222-8222-222222222222";
      const plan = {
        authorized: true as const,
        status: "authorized" as const,
        run_id: runUuid,
        owner_user_id: USER_ID,
        owner_platform_user_id: "wx_alice",
        platform: "weixin" as const,
        target_kind: "dm" as const,
        requested_target: "weixin:@me",
        canonical_target_address: "weixin:@me",
        recipient_id: "wx_alice",
        items: [
          {
            item_index: 0,
            delivery_key: "data_task:dt_partial_def",
            kind: "text_and_file" as const,
            text: "Caption text",
            artifact: {
              artifact_id: "art_pd",
              filename: "chart.png",
              mime_type: "image/png",
              size_bytes: pngBytes.byteLength,
              sha256: pngSha,
            },
          },
        ],
      };

      // 4a) I-6 claim_status="deferred" in DataTask -> returns deferred cleanly without sending
      vi.mocked(h.backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(plan);
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
        claim_status: "deferred",
        status: "deferred",
        code: "delivery_deferred",
        retryable: true,
        platform: "weixin",
        target_address: "weixin:@me",
        is_current: false,
      });
      const dtNotDue = await h.app.dataTaskNotifications.handleDataTaskNotification({
        run_id: runUuid,
        target: "weixin:@me",
      });
      expect(dtNotDue.data.status).toBe("deferred");
      expect(dtNotDue.data.code).toBe("delivery_deferred");

      // 4b) Text sends, file preflight defers -> `partial_deferred`
      const textCountBefore = h.weixinAdapter.sentTexts.length;
      const fileCountBefore = h.weixinAdapter.sentFiles.length;
      vi.mocked(h.backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(plan);
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
        claim_status: "claimed",
        platform: "weixin",
        target_address: "weixin:@me",
        recipient_id: "wx_alice",
        is_current: false,
        home_workspace_id: HOME_WS_ID,
        weixin_context_token: "ctx_1",
        delivery_attempt: 1,
      });
      vi.mocked(h.backend.downloadDataTaskRunArtifact).mockResolvedValueOnce({
        content: pngBytes,
        mimeType: "image/png",
        filename: "chart.png",
        sha256Header: pngSha,
      });
      vi.mocked(h.backend.preflightWeixinSend)
        .mockResolvedValueOnce({
          authorized: true,
          decision: "allow",
          retryable: false,
          chunk_index: 0,
          account_id: "wx_acc_1",
          ilink_bot_id: "bot_1",
          bot_generation: 1,
          bot_token: "tok_1",
          base_url: "https://ilinkai.weixin.qq.com",
          cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
          recipient_id: "wx_alice",
          weixin_context_token: "ctx_1",
          weixin_context_token_version: 1,
        })
        .mockResolvedValueOnce({
          authorized: false,
          decision: "defer",
          retryable: true,
          deny_code: "roster_lookup_error",
          message: "Roster down on file",
        });

      const dtPartDef = await h.app.dataTaskNotifications.handleDataTaskNotification({
        run_id: runUuid,
        target: "weixin:@me",
      });
      expect(dtPartDef.data.status).toBe("partial_deferred");
      expect(dtPartDef.data.items[0]?.status).toBe("partial_deferred");
      expect(h.weixinAdapter.sentTexts).toHaveLength(textCountBefore + 1);
      expect(h.weixinAdapter.sentFiles).toHaveLength(fileCountBefore);
      expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
        delivery_key: "data_task:dt_partial_def",
        status: "partial_deferred",
        outcome: "partial_deferred",
        sent_chunks: 1,
      });

      // 4c) Retry DataTask with `sent_chunks: 1` -> skips text and sends ONLY the chart file!
      vi.mocked(h.backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(plan);
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
        claim_status: "claimed",
        platform: "weixin",
        target_address: "weixin:@me",
        recipient_id: "wx_alice",
        is_current: false,
        home_workspace_id: HOME_WS_ID,
        weixin_context_token: "ctx_1",
        delivery_attempt: 2,
        sent_chunks: 1,
        platform_message_ids: ["wx_caption_0"],
        files_status: [],
        delivered_segments: [
          { kind: "text", index: 0, message_id: "wx_caption_0" },
        ],
      });
      vi.mocked(h.backend.downloadDataTaskRunArtifact).mockResolvedValueOnce({
        content: pngBytes,
        mimeType: "image/png",
        filename: "chart.png",
        sha256Header: pngSha,
      });
      const dtResumed = await h.app.dataTaskNotifications.handleDataTaskNotification({
        run_id: runUuid,
        target: "weixin:@me",
      });
      expect(dtResumed.data.status).toBe("sent");
      expect(h.weixinAdapter.sentTexts).toHaveLength(textCountBefore + 1); // text NOT re-sent
      expect(h.weixinAdapter.sentFiles).toHaveLength(fileCountBefore + 1); // file sent
    } finally {
      await h.cleanup();
    }
  });

  it("Decision #8: real undici connection pools load without injected factory and poll pool timeout exceeds 60s max long-poll", async () => {
    expect(WEIXIN_POLL_POOL_TIMEOUT_MS).toBeGreaterThan(60_000);
    const poolMgr = new WeixinHttpPoolManager({ maxConcurrentAccounts: 64 });
    try {
      await poolMgr.initAsync();
      const pollDispatcher = poolMgr.getPollDispatcher();
      const controlDispatcher = poolMgr.getControlDispatcher();
      expect(pollDispatcher).toBeDefined();
      expect(controlDispatcher).toBeDefined();
      expect(pollDispatcher).not.toBe(controlDispatcher);
      expect(typeof pollDispatcher?.close).toBe("function");
      expect(typeof controlDispatcher?.close).toBe("function");
      expect(poolMgr.getPollPoolConnections()).toBe(80);
      expect(poolMgr.getControlPoolConnections()).toBe(64);
    } finally {
      await poolMgr.close();
    }
  });

  it("Decision #9: Feishu & Discord omit X-Gateway-Epoch on I-6/I-7 while Weixin sends it, and Feishu & Discord retain 'partial' on chunk-2 unknown", async () => {
    // 1) HttpBackendClient header checks for I-6 / I-7 across weixin vs feishu/discord
    const seenHeaders: Array<{ url: string; epoch: string | null }> = [];
    const client = new HttpBackendClient({
      baseUrl: "http://127.0.0.1:8000",
      serviceToken: TEST_SERVICE_TOKEN,
      fetchImpl: async (input, init) => {
        const hdrs = new Headers(init?.headers);
        seenHeaders.push({
          url: String(input),
          epoch: hdrs.get("x-gateway-epoch"),
        });
        return new Response(
          JSON.stringify({
            ok: true,
            claim_status: "claimed",
            delivery_key: "k",
            status: "sent",
            sent_to_current_count: 1,
            reconcile_status: "consumed",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    client.setGatewayEpoch(7);

    // Feishu & Discord -> NO X-Gateway-Epoch on I-6 / I-7
    await client.authorizeAndClaimDelivery({
      delivery_key: "k_feishu",
      kind: "agent_send",
      platform: "feishu",
      target: "feishu:@me",
      has_non_empty_text: true,
      total_chunks: 1,
    });
    await client.authorizeAndClaimDelivery({
      delivery_key: "k_discord",
      kind: "agent_send",
      platform: "discord",
      target: "discord:@me",
      has_non_empty_text: true,
      total_chunks: 1,
    });
    await client.updateDeliveryProgress(
      { delivery_key: "k_feishu", status: "sent" },
      { platform: "feishu" },
    );
    await client.updateDeliveryProgress(
      { delivery_key: "k_discord", status: "sent" },
      { platform: "discord" },
    );
    await client.updateInboundProgress({
      platform: "feishu",
      message_id: "m_f",
      status: "completed",
    });
    await client.reconcileSteer({ platform: "discord", message_id: "m_d" });

    for (const entry of seenHeaders) {
      expect(entry.epoch).toBeNull();
    }

    // Weixin -> MUST include X-Gateway-Epoch: 7 on I-6 / I-7
    seenHeaders.length = 0;
    await client.authorizeAndClaimDelivery({
      delivery_key: "k_wx",
      kind: "agent_send",
      platform: "weixin",
      target: "weixin:@me",
      has_non_empty_text: true,
      total_chunks: 1,
    });
    await client.updateDeliveryProgress(
      { delivery_key: "k_wx", status: "sent" },
      { platform: "weixin" },
    );
    await client.updateInboundProgress({
      platform: "weixin",
      message_id: "m_w",
      status: "completed",
    });
    await client.reconcileSteer({ platform: "weixin", message_id: "m_w" });
    expect(seenHeaders.every((e) => e.epoch === "7")).toBe(true);

    // 2) Feishu & Discord chunk-2 `unknown` after chunk-1 success retains original `partial` settlement
    const h = createTestHarness();
    try {
      for (const adapter of [h.feishuAdapter, h.discordAdapter] as const) {
        let sendCalls = 0;
        adapter.setSendTextBehavior(async () => {
          sendCalls += 1;
          if (sendCalls === 1) return { messageId: `${adapter.platform}_chunk0` };
          throw new ChannelDeliveryError("Transient network drop", {
            outcome: "unknown",
            code: "unavailable",
          });
        });
        const res = await h.app.deliveryService.executeDelivery({
          deliveryKey: `del_${adapter.platform}_chunk2_unknown`,
          kind: "agent_send",
          userId: USER_ID,
          platform: adapter.platform,
          target: `${adapter.platform}:@me`,
          text: `${"F".repeat(1900)}\n${"G".repeat(1900)}`,
        });
        expect(res.status).toBe("partial");
        expect(h.backend.deliveryUpdates.at(-1)).toMatchObject({
          delivery_key: `del_${adapter.platform}_chunk2_unknown`,
          status: "partial",
        });
      }
    } finally {
      await h.cleanup();
    }
  });

  it("Decision #10 & #11: QR errors use fixed safe messages (never backend free text), and startup 503 allows read-only GET /agent/* while blocking writes & QR", async () => {
    // 1) Decision #10: backend free-text message is NEVER forwarded on QR error
    const qrBackend = createMockBackend();
    vi.mocked(qrBackend.patchWeixinLoginSession).mockRejectedValueOnce(
      new BackendHttpError("conflict", 409, {
        code: "ilink_user_already_bound",
        message: "LEAKED_INTERNAL_DB_ROW_SECRET_XYZ",
      }),
    );
    const qrMgr = new WeixinQrLoginManager({
      backend: qrBackend,
      checkResolvedIps: false,
      fetchImpl: async () =>
        new Response(
          JSON.stringify({
            ret: 0,
            qrcode: "qr_tok_safe",
            qrcode_img_content: "https://ilinkai.weixin.qq.com/qr/safe",
          }),
          { status: 200 },
        ),
    });
    try {
      await qrMgr.startQrLogin(USER_ID, { sessionKey: "sess_err_1", loginVersion: 1 });
      expect.unreachable("Expected WeixinQrLoginError");
    } catch (err) {
      expect(err).toBeInstanceOf(WeixinQrLoginError);
      const qrErr = err as WeixinQrLoginError;
      expect(qrErr.code).toBe("ilink_user_already_bound");
      expect(qrErr.message).not.toContain("LEAKED_INTERNAL_DB_ROW_SECRET_XYZ");
      expect(qrErr.message).toContain("already bound");
    }

    // 2) Decision #11: While waiting for lease, read-only GET /agent/* returns 200 while write/QR returns 503
    const backend = createMockBackend();
    const leaseEntered = defer<void>();
    const releaseLease = defer<void>();
    vi.mocked(backend.acquireGatewayLease).mockImplementation(async (req) => {
      leaseEntered.resolve();
      await releaseLease.promise;
      return {
        granted: true,
        epoch: 1,
        expires_at: new Date(Date.now() + req.ttl_seconds * 1000).toISOString(),
      };
    });
    const config = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      DALTON_BACKEND_URL: "http://127.0.0.1:8000",
      WEB_BASE_URL: "https://app.ginlix.ai",
    });
    const feishu = new (await import("../src/channels/fake-adapter.js")).FakeChannelAdapter("feishu", 2000);
    const app = createGatewayApp({
      config,
      backend,
      adapters: new Map([["feishu", feishu]]),
      enableLease: true,
    });
    const startPromise = app.start();

    try {
      await waitBounded(leaseEntered.promise, "lease acquire entered");
      const addr = app.server.address();
      if (!addr || typeof addr === "string") {
        throw new Error("Expected gateway server to be listening on TCP port");
      }
      const authHeaders = {
        "X-Service-Token": TEST_SERVICE_TOKEN,
        "X-User-Id": USER_ID,
      };
      const base = `http://${addr.address}:${addr.port}`;

      // Read-only routes return 200 while acquiring lease
      const healthRes = await fetchGatewayServer(app, `${base}/health`, {
        method: "GET",
        headers: authHeaders,
      });
      expect(healthRes.status).toBe(200);

      const settingsRes = await fetchGatewayServer(app, `${base}/agent/settings`, {
        method: "GET",
        headers: authHeaders,
      });
      expect(settingsRes.status).toBe(200);

      const targetsRes = await fetchGatewayServer(app, `${base}/agent/targets`, {
        method: "GET",
        headers: authHeaders,
      });
      expect(targetsRes.status).toBe(200);

      const availRes = await fetchGatewayServer(
        app,
        `${base}/agent/settings/available`,
        { method: "GET", headers: authHeaders },
      );
      expect(availRes.status).toBe(200);

      const autoTargetsRes = await fetchGatewayServer(
        app,
        `${base}/agent/automation-targets`,
        { method: "GET", headers: authHeaders },
      );
      expect(autoTargetsRes.status).toBe(200);

      // Write & iLink routes return 503 gateway_starting
      const putSettingsRes = await fetchGatewayServer(
        app,
        `${base}/agent/settings`,
        {
          method: "PUT",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          body: JSON.stringify({ version: "1", settings: {} }),
        },
      );
      expect(putSettingsRes.status).toBe(503);
      expect(await putSettingsRes.json()).toMatchObject({
        code: "gateway_starting",
      });

      const qrRes = await fetchGatewayServer(
        app,
        `${base}/internal/weixin/qrcode/start`,
        {
          method: "POST",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: USER_ID }),
        },
      );
      expect(qrRes.status).toBe(503);
    } finally {
      releaseLease.resolve();
      await startPromise;
      await app.stop();
    }
  });

  it("Round 3 Supplement #4: polls GET /internal/deliveries/deferred-due (30s default), re-claims partial_deferred under original delivery_key sending only remaining chunks, and stops immediately on lease loss", async () => {
    expect(DEFAULT_DEFERRED_DELIVERY_POLL_INTERVAL_MS).toBe(30_000);

    const backend = createMockBackend();
    const weixin = new FakeChannelAdapter("weixin", 100);
    const deliveryKey = "run_deferred_replay_1:tc_1";
    const twoChunkText = `${"A".repeat(100)}${"B".repeat(40)}`;

    // Track simulated backend I-6/I-7 ledger for `deliveryKey`
    let claimCount = 0;
    let storedSentChunks = 0;
    let storedMsgIds: string[] = [];
    let storedSegments: unknown[] = [];
    let storedStatus: "in_flight" | "partial_deferred" | "sent" = "in_flight";
    let dueReady = false;
    const replayFinished = defer<void>();

    vi.mocked(backend.authorizeAndClaimDelivery).mockImplementation(async (req) => {
      claimCount += 1;
      if (req.delivery_key === deliveryKey) {
        return {
          claim_status: "claimed",
          platform: "weixin",
          target_address: "weixin:wx_u1@im.wechat",
          recipient_id: "wx_u1@im.wechat",
          is_current: true,
          delivery_attempt: claimCount,
          sent_chunks: storedSentChunks,
          total_chunks: 2,
          platform_message_ids: [...storedMsgIds],
          files_status: [],
          delivered_segments: [...storedSegments],
          weixin_account_id: "wx_acc_1",
          weixin_bot_id: "bot_1",
          weixin_bot_generation: 1,
          weixin_context_token: "ctx_tok_1",
        };
      }
      return {
        claim_status: "claimed",
        platform: "weixin",
        target_address: req.target ?? "weixin:@me",
        recipient_id: "wx_u1@im.wechat",
        is_current: true,
        delivery_attempt: claimCount,
      };
    });

    vi.mocked(backend.updateDeliveryProgress).mockImplementation(async (req) => {
      if (req.delivery_key === deliveryKey) {
        if (typeof req.sent_chunks === "number") {
          storedSentChunks = req.sent_chunks;
        }
        if (Array.isArray(req.platform_message_ids)) {
          storedMsgIds = [...req.platform_message_ids];
        }
        if (Array.isArray(req.delivered_segments)) {
          storedSegments = [...req.delivered_segments];
        }
        if (req.status === "partial_deferred") {
          storedStatus = "partial_deferred";
        } else if (req.status === "sent") {
          storedStatus = "sent";
          dueReady = false;
          replayFinished.resolve();
        }
      }
      return {
        ok: true,
        delivery_key: req.delivery_key,
        status: req.status ?? storedStatus,
        sent_to_current_count: req.status === "sent" ? 1 : 0,
      };
    });

    // Chunk 0 succeeds; chunk 1 initially defers, then succeeds when replayed
    let chunk1DeferredOnce = false;
    vi.mocked(backend.preflightWeixinSend).mockImplementation(async (_key, req) => {
      if (req.chunk_index === 1 && !chunk1DeferredOnce) {
        chunk1DeferredOnce = true;
        return {
          authorized: false,
          decision: "defer",
          retryable: true,
          deny_code: "roster_lookup_error",
          message: "Roster lookup transient failure",
        };
      }
      return {
        authorized: true,
        decision: "allow",
        retryable: false,
        chunk_index: req.chunk_index ?? 0,
        account_id: "wx_acc_1",
        ilink_bot_id: "bot_1",
        bot_generation: 1,
        bot_token: "bot_tok_1",
        base_url: "https://ilinkai.weixin.qq.com",
        cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
        recipient_id: "wx_u1@im.wechat",
        weixin_context_token: "ctx_tok_1",
        weixin_context_token_version: 1,
      };
    });

    vi.mocked(backend.getDueDeferredDeliveries!).mockImplementation(async () => {
      if (!dueReady) {
        return {
          ok: true,
          expired_count: 0,
          next_upcoming_retry_at: null,
          deliveries: [],
        };
      }
      return {
        ok: true,
        expired_count: 0,
        next_upcoming_retry_at: null,
        deliveries: [
          {
            delivery_key: deliveryKey,
            kind: "agent_send",
            user_id: USER_ID,
            platform: "weixin",
            target_address: "weixin:wx_u1@im.wechat",
            status: "partial_deferred",
            defer_count: 1,
            sent_chunks: storedSentChunks,
            total_chunks: 2,
            platform_message_ids: [...storedMsgIds],
            files_status: [],
            delivered_segments: [...storedSegments],
            delivery_attempt: 1,
            weixin_account_id: "wx_acc_1",
            weixin_bot_id: "bot_1",
            weixin_bot_generation: 1,
            weixin_recipient_id: "wx_u1@im.wechat",
          },
        ],
      };
    });

    let leaseLostReason: string | null = null;
    const leaseLostDeferred = defer<string>();
    const config = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      DALTON_WEIXIN_ENABLED: "true",
    });
    const app = createGatewayApp({
      config,
      backend,
      adapters: new Map([["weixin", weixin]]),
      enableLease: true,
      deferredDeliveryPollIntervalMs: 30,
      onLeaseLost: (reason) => {
        leaseLostReason = reason;
        leaseLostDeferred.resolve(reason);
      },
    });

    try {
      await app.start();
      expect(app.deferredDeliveryPoller?.isRunning).toBe(true);

      // Initial delivery: chunk 0 delivers, chunk 1 defers -> partial_deferred
      const initialRes = await app.deliveryService.executeDelivery({
        deliveryKey,
        kind: "agent_send",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:wx_u1@im.wechat",
        text: twoChunkText,
      });
      expect(initialRes.status).toBe("partial_deferred");
      expect(weixin.sentTexts).toHaveLength(1);
      expect(weixin.sentTexts[0]?.chunkIndex).toBe(0);
      expect(app.deliveryService.hasDeferredPayload(deliveryKey)).toBe(true);

      // Make the deferred delivery due on the backend; poller should re-claim and send ONLY chunk 1
      dueReady = true;
      await waitBounded(replayFinished.promise, "deferred delivery replay finished");

      expect(backend.getDueDeferredDeliveries).toHaveBeenCalledWith({ limit: 50 });
      expect(claimCount).toBe(2);
      expect(weixin.sentTexts).toHaveLength(2);
      expect(weixin.sentTexts[0]?.chunkIndex).toBe(0);
      expect(weixin.sentTexts[0]?.text).toBe("A".repeat(100));
      expect(weixin.sentTexts[1]?.chunkIndex).toBe(1);
      expect(weixin.sentTexts[1]?.text).toBe("B".repeat(40));
      expect(storedStatus).toBe("sent");
      expect(app.deliveryService.hasDeferredPayload(deliveryKey)).toBe(false);

      // Trigger lease loss via 409 stale_gateway_epoch on next poll -> poller stops immediately
      vi.mocked(backend.getDueDeferredDeliveries!).mockRejectedValueOnce(
        new StaleGatewayEpochError("Gateway lease epoch is stale", {
          status: 409,
          epoch: 1,
        }),
      );
      await waitBounded(
        leaseLostDeferred.promise,
        "deferred poller stopped on 409 stale epoch",
      );
      expect(leaseLostReason).toBe("deferred_poll_stale_epoch");
      expect(app.deferredDeliveryPoller?.isRunning).toBe(false);
      const callsAtStop = vi.mocked(backend.getDueDeferredDeliveries!).mock.calls.length;
      await new Promise((r) => setTimeout(r, 110));
      expect(vi.mocked(backend.getDueDeferredDeliveries!).mock.calls.length).toBe(
        callsAtStop,
      );
    } finally {
      await app.stop();
    }
  });

  it("Round 3 Supplement #4: DeferredDeliveryPoller skips ticks during safety margin without stopping, and routes due data_task rows to DataTaskNotificationService", async () => {
    const backend = createMockBackend();
    const handleDataTaskNotification = vi.fn(async () => ({
      status: 200,
      data: {
        ok: true,
        status: "sent" as const,
        code: null,
        message: "ok",
        run_id: "11111111-1111-4111-8111-111111111111",
        target: "weixin:wx_u1@im.wechat",
        platform: "weixin" as const,
        items_total: 1,
        items_sent: 1,
        items: [],
      },
    }));
    const leaseManager = new GatewayLeaseManager({
      backend,
      ttlSeconds: 30,
      heartbeatIntervalMs: 60_000,
      safetyMarginMs: 5_000,
    });
    // Acquire with only 2s remaining (inside 5s safety margin, not expired)
    vi.mocked(backend.acquireGatewayLease).mockResolvedValueOnce({
      granted: true,
      epoch: 7,
      expires_at: new Date(Date.now() + 2_000).toISOString(),
    });
    await leaseManager.acquireUntilGranted();

    const poller = new DeferredDeliveryPoller({
      backend,
      deliveryService: {
        hasDeferredPayload: () => false,
        replayDeferredDelivery: async () => null,
      },
      dataTaskNotifications: {
        handleDataTaskNotification,
      },
      automationNotifications: {
        handle: vi.fn(async () => ({
          status: 200,
          data: {
            ok: true,
            status: "sent" as const,
            code: null,
            message: "ok",
            run_id: "11111111-1111-4111-8111-111111111111",
            target: "weixin:wx_u1@im.wechat",
            platform: "weixin" as const,
            items_total: 0,
            items_sent: 0,
            items: [],
          },
        })),
      },
      leaseManager,
      intervalMs: 60_000,
    });

    try {
      poller.start();
      // In safety margin: pollOnce skips without calling backend and keeps poller running
      const skipped = await poller.pollOnce();
      expect(skipped).toBe(0);
      expect(backend.getDueDeferredDeliveries).not.toHaveBeenCalled();
      expect(poller.isRunning).toBe(true);

      // Refresh lease beyond safety margin -> pollOnce now calls backend and routes data_task
      vi.mocked(backend.heartbeatGatewayLease).mockResolvedValueOnce({
        ok: true,
        epoch: 7,
        expires_at: new Date(Date.now() + 30_000).toISOString(),
      });
      await leaseManager.runHeartbeatOnce();
      vi.mocked(backend.getDueDeferredDeliveries!).mockResolvedValueOnce({
        ok: true,
        expired_count: 0,
        next_upcoming_retry_at: null,
        deliveries: [
          {
            delivery_key: "data_task:11111111-1111-4111-8111-111111111111:item_0",
            kind: "data_task",
            run_id: "11111111-1111-4111-8111-111111111111",
            platform: "weixin",
            target_address: "weixin:wx_u1@im.wechat",
            status: "partial_deferred",
            defer_count: 1,
            sent_chunks: 1,
            total_chunks: 2,
          },
        ],
      });

      const replayed = await poller.pollOnce();
      expect(replayed).toBe(1);
      expect(handleDataTaskNotification).toHaveBeenCalledTimes(1);
      expect(handleDataTaskNotification).toHaveBeenCalledWith({
        run_id: "11111111-1111-4111-8111-111111111111",
        target: "weixin:wx_u1@im.wechat",
      });
    } finally {
      poller.stop();
      await leaseManager.stopAndRelease();
    }
  });
});
