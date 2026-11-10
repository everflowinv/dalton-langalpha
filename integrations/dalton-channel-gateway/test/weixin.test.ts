import { describe, expect, it, vi } from "vitest";
import {
  aesEcbPaddedSize,
  buildWeixinScopedMessageId,
  decryptAes128Ecb,
  downloadWeixinCdnAttachment,
  encodeOutboundAesKeyBase64,
  encryptAes128Ecb,
  filterWeixinMarkdown,
  isSessionExpiredCode,
  normalizeAndValidateCdnBaseUrl,
  normalizeAndValidateIlinkBaseUrl,
  parseInboundAesKey,
  parseRetryAfterMs,
  parseWeixinApiJson,
  parseWeixinInboundMessage,
  resolvePersistedAttachmentMeta,
  resolveValidatedCdnDownloadUrl,
  resolveValidatedRedirectBaseUrl,
  sendWeixinMediaAttachment,
  sendWeixinTextMessage,
  WeixinChannelAdapter,
  WeixinQrLoginManager,
} from "../src/channels/weixin/index.js";
import { sanitizePublicWeixinQrResponse } from "../src/server.js";
import {
  MAX_CIPHERTEXT_ATTACHMENT_BYTES,
  SafeFetchError,
  safeFetchResponse,
} from "../src/shared/safe-fetch.js";
import { MAX_ATTACHMENT_BYTES } from "../src/types.js";
import type {
  I1RegisterAndResolveRequest,
  I1RegisterAndResolveResponse,
  I9WeixinStateGetResponse,
  I9WeixinStatePutResponse,
} from "../src/backend-client.js";
import type { InboundChannelEvent } from "../src/types.js";
import { createTestHarness, USER_ID } from "./helpers.js";

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
    ...extra,
  };
}

function weixinState(
  overrides: Partial<I9WeixinStateGetResponse> = {},
): I9WeixinStateGetResponse {
  return {
    configured: true,
    status: "active",
    bot_token: "bot_tok_live",
    ilink_bot_id: "bot_live_1",
    ilink_user_id: "wx_owner",
    owner_user_id: USER_ID,
    base_url: "https://ilinkai.weixin.qq.com",
    get_updates_buf: "buf_0",
    bot_generation: 5,
    updated_at: "2026-01-01T00:00:00.000Z",
    weixin_context_token: "ctx_live",
    ...overrides,
  };
}

function weixinPut(
  overrides: Partial<I9WeixinStatePutResponse> = {},
): I9WeixinStatePutResponse {
  return {
    ok: true,
    updated: true,
    ilink_bot_id: "bot_live_1",
    owner_user_id: USER_ID,
    base_url: "https://ilinkai.weixin.qq.com",
    get_updates_buf: "buf_0",
    status: "active",
    updated_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
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

    // 1. Hex string from image_item.aeskey
    const k1 = parseInboundAesKey({ aesKeyHex: keyHex });
    expect(k1.equals(key)).toBe(true);

    // 2. base64(16 raw bytes) from media.aes_key
    const k2 = parseInboundAesKey({ aesKeyBase64: key.toString("base64") });
    expect(k2.equals(key)).toBe(true);

    // 3. base64(32 hex ASCII chars) from media.aes_key
    const k3 = parseInboundAesKey({
      aesKeyBase64: Buffer.from(keyHex, "utf8").toString("base64"),
    });
    expect(k3.equals(key)).toBe(true);
    expect(encodeOutboundAesKeyBase64(key)).toBe(
      Buffer.from(keyHex, "utf8").toString("base64"),
    );

    // 4. Plaintext image fallback when no key is provided (CDN headers isolated)
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

    // 5. Wrong AES key / bad padding is rejected cleanly
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

    // 1. Validly encrypted attachment whose decrypted plaintext size (32 bytes) differs from declared sizeBytes (1024) is rejected on size mismatch
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

    // 2. Truncated ciphertext with invalid PKCS#7 padding is rejected during decryption
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

    // Reject HTTP
    expect(() =>
      normalizeAndValidateIlinkBaseUrl(
        "http://ilinkai.weixin.qq.com",
        allowedApi,
      ),
    ).toThrow(/HTTPS/i);

    // Reject embedded credentials
    expect(() =>
      resolveValidatedCdnDownloadUrl({
        fullUrl: "https://user:secret@novac2c.cdn.weixin.qq.com/c2c/download",
        allowedCdnDomains: allowedCdn,
      }),
    ).toThrow(/Embedded credentials/i);

    // Reject disallowed host with structured SafeFetchError (`disallowed_domain`)
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

    // Reject a literal private IP before any DNS lookup (`blocked_host`)
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

  it("parses mixed text + voice transcription in order, isolates inline quotes from slash commands, handles voice without transcription, and reconstructs attachments from persisted rawPayload", () => {
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
      ilinkBotId: "ilink_bot_1",
      botGeneration: 3,
    });

    expect(event.chatType).toBe("dm");
    expect(event.messageId).toBe(
      buildWeixinScopedMessageId("ilink_bot_1", msg),
    );
    // Same bot re-login produces identical dedup ID; different bot produces distinct ID
    expect(buildWeixinScopedMessageId("ilink_bot_1", msg)).not.toBe(
      buildWeixinScopedMessageId("ilink_bot_2", msg),
    );

    // Inline quote is NOT in event.commandText (so quoted /new cannot trigger slash command),
    // but IS included in event.text for the LLM turn
    expect(event.commandText).toBe(
      "First segment\nSecond segment from voice transcription",
    );
    expect(event.commandText?.startsWith("/new")).toBe(false);
    expect(event.text).toContain(
      "> [引用] Previous turn | /new should not trigger command",
    );
    expect(event.text).toContain(
      "First segment\nSecond segment from voice transcription",
    );

    // Attachment metadata is reconstructible from persisted rawPayload alone
    expect(event.attachments).toBeDefined();
    expect(event.attachments).toHaveLength(1);
    const descriptor = event.attachments?.[0];
    expect(descriptor).toBeDefined();
    if (!descriptor) {
      throw new Error("Expected attachment descriptor");
    }
    expect(descriptor.id).toBe(`${event.messageId}:att:0`);
    const reconstructed = resolvePersistedAttachmentMeta(descriptor, {
      ...event,
      rawPayload: JSON.parse(JSON.stringify(event.rawPayload)),
    });
    expect(reconstructed?.encryptQueryParam).toBe("enc_img_1");
    expect(reconstructed?.aesKeyHex).toBe("00112233445566778899aabbccddeeff");

    // Voice without transcription sets explainable notice flag
    const noTransVoice = parseWeixinInboundMessage(
      {
        message_id: "18446744073709551611",
        from_user_id: "wx_user_alice",
        item_list: [{ type: 3, voice_item: {} }],
      },
      {
        ilinkBotId: "ilink_bot_1",
        botGeneration: 3,
      },
    );
    expect(noTransVoice.voiceWithoutTranscription).toBe(true);

    // Non-empty group_id is rejected as group chat before any command/media processing
    const groupMsg = parseWeixinInboundMessage(
      {
        message_id: "18446744073709551612",
        from_user_id: "wx_user_alice",
        group_id: "wx_group_999",
        item_list: [{ type: 1, text_item: { text: "/new" } }],
      },
      {
        ilinkBotId: "ilink_bot_1",
        botGeneration: 3,
      },
    );
    expect(groupMsg.chatType).toBe("group");
    expect(groupMsg.rawPayload?.rejected_reason).toBe(
      "weixin_group_unsupported",
    );

    // Missing message_id gets a deterministic rejection ID
    const missingId1 = parseWeixinInboundMessage(
      {
        from_user_id: "wx_user_alice",
        create_time_ms: 1700000000000,
        item_list: [{ type: 1, text_item: { text: "hello" } }],
      },
      {
        ilinkBotId: "ilink_bot_1",
        botGeneration: 3,
      },
    );
    const missingId2 = parseWeixinInboundMessage(
      {
        from_user_id: "wx_user_alice",
        create_time_ms: 1700000000000,
        item_list: [{ type: 1, text_item: { text: "hello" } }],
      },
      {
        ilinkBotId: "ilink_bot_1",
        botGeneration: 3,
      },
    );
    expect(missingId1.rawPayload?.invalid_inbound_event).toBe(true);
    expect(missingId1.messageId).toBe(missingId2.messageId);
    expect(missingId1.weixinContextSeq).toBeUndefined();
    expect(missingId1.rawPayload?.create_time_ms).toBe(1700000000000);
    expect(missingId1.rawPayload?.seq).toBeUndefined();

    const protocolAndTime = parseWeixinInboundMessage(
      {
        message_id: "100",
        seq: 1700000000000,
        from_user_id: "wx_user_alice",
        create_time_ms: 1800000000000,
        item_list: [{ type: 1, text_item: { text: "ordered" } }],
      },
      { ilinkBotId: "ilink_bot_1", botGeneration: 3, batchSeq: Date.now() * 1000 },
    );
    expect(protocolAndTime.weixinContextSeq).toBe(1700000000000);
    expect(protocolAndTime.rawPayload?.seq).toBe(1700000000000);
    expect(protocolAndTime.rawPayload?.create_time_ms).toBe(1800000000000);

    const timeOnly = parseWeixinInboundMessage(
      {
        message_id: "101",
        from_user_id: "wx_user_alice",
        create_time_ms: 1800000000000,
        item_list: [{ type: 1, text_item: { text: "timed" } }],
      },
      { ilinkBotId: "ilink_bot_1", botGeneration: 3, batchSeq: 999 },
    );
    expect(timeOnly.weixinContextSeq).toBeUndefined();
    expect(timeOnly.rawPayload?.seq).toBeUndefined();
    expect(timeOnly.rawPayload?.create_time_ms).toBe(1800000000000);
  });

  it("handles concurrent admin QR logins, stale confirmation rejection, duplicate wait idempotency, redirect_host validation, and public field whitelisting", async () => {
    const h = createTestHarness();
    try {
      let qrCounter = 0;
      let currentBotGen = 1;
      let activeSessionKey: string | null = null;
      let activeOwnerUserId: string | null = null;
      let activeQrcode: string | null = null;
      let activeStatus: string | null = null;
      let activeLoginVersion = 0;
      let activeExpiresAt: string | null = null;
      let confirmedResult: Record<string, unknown> | null = null;

      vi.mocked(h.backend.getWeixinState).mockImplementation(async () => ({
        configured: currentBotGen > 1,
        status: currentBotGen > 1 ? "active" : "unconfigured",
        bot_token: currentBotGen > 1 ? "tok_old" : null,
        ilink_bot_id: currentBotGen > 1 ? "bot_admin_2" : null,
        ilink_user_id: currentBotGen > 1 ? "wx_admin_2" : null,
        owner_user_id: currentBotGen > 1 ? "admin_2" : null,
        base_url: "https://ilinkai.weixin.qq.com",
        get_updates_buf: "",
        bot_generation: currentBotGen,
        updated_at: new Date().toISOString(),
        login_session_key: activeSessionKey,
        login_owner_user_id: activeOwnerUserId,
        login_version: activeLoginVersion,
        login_expected_bot_generation: currentBotGen,
        login_status: activeStatus,
        login_qrcode: activeQrcode,
        login_redirect_base_url: "https://ilinkai.weixin.qq.com",
        login_expires_at: activeExpiresAt,
        login_confirmed_result: confirmedResult,
      }));

      vi.mocked(h.backend.putWeixinState).mockImplementation(async (req) => {
        if (req.login_session_action === "start") {
          activeLoginVersion += 1;
          activeSessionKey = req.login_session_key ?? null;
          activeOwnerUserId = req.login_owner_user_id ?? null;
          activeQrcode = req.login_qrcode ?? null;
          activeStatus = "wait";
          activeExpiresAt = req.login_expires_at ?? null;
          confirmedResult = null;
          return {
            ok: true,
            updated: true,
            stale_session: false,
            ilink_bot_id: null,
            owner_user_id: activeOwnerUserId,
            base_url: "https://ilinkai.weixin.qq.com",
            get_updates_buf: "",
            status: "unconfigured",
            bot_generation: currentBotGen,
            login_version: activeLoginVersion,
            updated_at: new Date().toISOString(),
          };
        }
        if (req.login_session_action === "confirm") {
          if (req.login_session_key !== activeSessionKey) {
            return {
              ok: false,
              updated: false,
              stale_session: true,
              ilink_bot_id: null,
              owner_user_id: activeOwnerUserId,
              base_url: "https://ilinkai.weixin.qq.com",
              get_updates_buf: "",
              status: "unconfigured",
              bot_generation: currentBotGen,
              updated_at: new Date().toISOString(),
            };
          }
          if (activeStatus === "confirmed" && confirmedResult) {
            return {
              ok: true,
              updated: false,
              idempotent: true,
              stale_session: false,
              confirmed_result: confirmedResult,
              ilink_bot_id: String(confirmedResult.ilink_bot_id ?? "bot_admin_2"),
              owner_user_id: activeOwnerUserId,
              base_url: "https://ilinkai.weixin.qq.com",
              get_updates_buf: "",
              status: "active",
              bot_generation: currentBotGen,
              updated_at: new Date().toISOString(),
            };
          }
          currentBotGen += 1;
          activeStatus = "confirmed";
          confirmedResult = {
            session_key: activeSessionKey,
            status: "confirmed",
            connected: true,
            ilink_bot_id: req.ilink_bot_id ?? "bot_admin_2",
            ilink_user_id: req.ilink_user_id ?? "wx_admin_2",
            bot_generation: currentBotGen,
          };
          return {
            ok: true,
            updated: true,
            stale_session: false,
            confirmed_result: confirmedResult,
            ilink_bot_id: req.ilink_bot_id ?? "bot_admin_2",
            owner_user_id: req.owner_user_id ?? activeOwnerUserId,
            base_url: req.base_url ?? "https://ilinkai.weixin.qq.com",
            get_updates_buf: "",
            status: "active",
            bot_generation: currentBotGen,
            updated_at: new Date().toISOString(),
          };
        }
        return {
          ok: true,
          updated: true,
          stale_session: false,
          ilink_bot_id: null,
          owner_user_id: activeOwnerUserId,
          base_url: "https://ilinkai.weixin.qq.com",
          get_updates_buf: "",
          status: "unconfigured",
          bot_generation: currentBotGen,
          updated_at: new Date().toISOString(),
        };
      });

      const fakeFetch: typeof fetch = vi.fn(async (input) => {
        const url = String(input);
        if (url.includes("get_bot_qrcode")) {
          qrCounter += 1;
          return makeJsonResponse({
            ret: 0,
            qrcode: `qr_ticket_${qrCounter}`,
            qrcode_img_content: `https://ilinkai.weixin.qq.com/qr/${qrCounter}?secret=never_leak`,
          });
        }
        if (url.includes("qrcode=qr_ticket_1")) {
          return makeJsonResponse({
            ret: 0,
            status: "confirmed",
            bot_token: "secret_token_admin_1",
            ilink_bot_id: "bot_admin_1",
            ilink_user_id: "wx_admin_1",
            baseurl: "https://ilinkai.weixin.qq.com",
          });
        }
        if (url.includes("qrcode=qr_ticket_2")) {
          return makeJsonResponse({
            ret: 0,
            status: "confirmed",
            bot_token: "secret_token_admin_2",
            ilink_bot_id: "bot_admin_2",
            ilink_user_id: "wx_admin_2",
            baseurl: "https://ilinkai.weixin.qq.com",
          });
        }
        throw new Error(`Unexpected URL: ${url}`);
      });

      const activatedStates: unknown[] = [];
      const qrManager = new WeixinQrLoginManager({
        backend: h.backend,
        baseUrl: "https://ilinkai.weixin.qq.com",
        allowedApiDomains: ["ilinkai.weixin.qq.com", "weixin.qq.com"],
        fetchImpl: fakeFetch,
        checkResolvedIps: false,
        onAccountConfirmed: async (state) => {
          activatedStates.push(state);
        },
      });

      // Admin 1 starts QR login
      const start1 = await qrManager.startQrLogin("admin_1");
      expect(start1.status).toBe("wait");
      // Query parameter secret is stripped from qrcode_url
      expect(String(start1.qrcode_url)).not.toContain("never_leak");

      // Admin 2 starts QR login immediately, superseding Admin 1
      const start2 = await qrManager.startQrLogin("admin_2");
      expect(start2.session_key).not.toBe(start1.session_key);

      // Admin 1's wait returns expired/superseded and never activates Admin 1's credentials
      const wait1 = await qrManager.waitQrLogin("admin_1", start1.session_key);
      expect(wait1.status).toBe("expired");
      expect(activatedStates).toHaveLength(0);

      // Wrong user cannot poll Admin 2's session
      await expect(
        qrManager.waitQrLogin("admin_1", start2.session_key),
      ).rejects.toMatchObject({ statusCode: 403 });

      // Admin 2's wait succeeds and activates once
      const wait2 = await qrManager.waitQrLogin("admin_2", start2.session_key);
      expect(wait2.status).toBe("confirmed");
      expect(wait2.connected).toBe(true);
      expect(wait2).not.toHaveProperty("bot_token");
      expect(activatedStates).toHaveLength(1);

      // A repeated wait reads the confirmed account again and applies the same generation.
      const wait2Dup = await qrManager.waitQrLogin(
        "admin_2",
        start2.session_key,
      );
      expect(wait2Dup.status).toBe("confirmed");
      expect(wait2Dup).not.toHaveProperty("bot_token");
      expect(activatedStates).toHaveLength(2);
      expect(activatedStates[1]).toMatchObject({
        ilinkBotId: (activatedStates[0] as { ilinkBotId: string }).ilinkBotId,
        botGeneration: (activatedStates[0] as { botGeneration: number })
          .botGeneration,
      });

      const savedBotId = (activatedStates[0] as { ilinkBotId: string }).ilinkBotId;
      vi.mocked(h.backend.getWeixinState).mockImplementation(async () => ({
        configured: true,
        status: "active",
        bot_token: "tok_replaced",
        ilink_bot_id: "bot_replaced",
        ilink_user_id: "wx_admin_2",
        owner_user_id: "admin_2",
        base_url: "https://ilinkai.weixin.qq.com",
        get_updates_buf: "",
        bot_generation: currentBotGen + 1,
        updated_at: new Date().toISOString(),
        login_session_key: activeSessionKey,
        login_owner_user_id: activeOwnerUserId,
        login_version: activeLoginVersion,
        login_expected_bot_generation: currentBotGen,
        login_status: "confirmed",
        login_qrcode: activeQrcode,
        login_redirect_base_url: "https://ilinkai.weixin.qq.com",
        login_expires_at: activeExpiresAt,
        login_confirmed_result: confirmedResult,
      }));
      const waitReplaced = await qrManager.waitQrLogin(
        "admin_2",
        start2.session_key,
      );
      expect(waitReplaced.status).toBe("expired");
      expect(waitReplaced).not.toHaveProperty("bot_token");
      expect(activatedStates).toHaveLength(2);
      expect(savedBotId).toBe("bot_admin_2");

      // Verify sanitizePublicWeixinQrResponse strips bot_token, context_token, and URL query/credentials
      const sanitized = sanitizePublicWeixinQrResponse({
        ...wait2,
        qrcode_url: "https://user:pass@ilinkai.weixin.qq.com/qr/2?token=secret",
        bot_token: "MUST_NOT_APPEAR",
        context_token: "MUST_NOT_APPEAR",
      });
      expect(sanitized).not.toHaveProperty("bot_token");
      expect(sanitized).not.toHaveProperty("context_token");
      expect(sanitized).not.toHaveProperty("qrcode_url");
    } finally {
      await h.cleanup();
    }
  });

  it("advances cursor in I-9 only after all batch messages are durably registered, marks expired on -14, and stops cleanly", async () => {
    const h = createTestHarness();
    try {
      let pollCount = 0;
      const cursorSaves: string[] = [];
      let expiredSaved = false;

      vi.mocked(h.backend.getWeixinState).mockImplementation(async () => ({
        configured: true,
        status: expiredSaved ? "expired" : "active",
        bot_token: "bot_tok_live",
        ilink_bot_id: "bot_live_1",
        ilink_user_id: "wx_owner",
        owner_user_id: USER_ID,
        base_url: "https://ilinkai.weixin.qq.com",
        get_updates_buf: cursorSaves[cursorSaves.length - 1] ?? "buf_0",
        bot_generation: 5,
        updated_at: new Date().toISOString(),
      }));

      vi.mocked(h.backend.putWeixinState).mockImplementation(async (req) => {
        if (req.status === "expired") {
          expiredSaved = true;
        }
        if (typeof req.get_updates_buf === "string" && !req.status) {
          cursorSaves.push(req.get_updates_buf);
        }
        return {
          ok: true,
          updated: true,
          ilink_bot_id: "bot_live_1",
          owner_user_id: USER_ID,
          base_url: "https://ilinkai.weixin.qq.com",
          get_updates_buf: req.get_updates_buf ?? "buf_0",
          status: req.status ?? "active",
          bot_generation: 5,
          updated_at: new Date().toISOString(),
        };
      });

      let failSecondMsgI1 = true;
      const seenMsgIds: string[] = [];

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
          // Third poll returns -14 session expired
          return makeJsonResponse({
            ret: -14,
            errcode: -14,
            errmsg: "session expired",
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
        fetchImpl: fakeFetch,
        checkResolvedIps: false,
      });

      adapter.onInboundEvent(async (ev) => {
        seenMsgIds.push(ev.messageId);
        if (ev.messageId.endsWith(":1002") && failSecondMsgI1) {
          failSecondMsgI1 = false;
          throw new Error("Simulated transient I-1 DB error on second message");
        }
      });

      await adapter.start();
      for (let i = 0; i < 40 && !expiredSaved; i += 1) {
        await new Promise((r) => setTimeout(r, 15));
      }

      expect(cursorSaves).toEqual(["buf_1"]);
      expect(expiredSaved).toBe(true);
      expect(adapter.isConnected()).toBe(false);
      await adapter.stop();
    } finally {
      await h.cleanup();
    }
  });

  it("classifies outbound lost response as unknown, business error as failed, 429 with Retry-After as rate_limited, and reuses CDN upload parameters on retry", async () => {
    // 1. Missing context_token fails immediately without network request
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
    ).rejects.toMatchObject({
      outcome: "failed",
    });
    expect(neverFetch).not.toHaveBeenCalled();

    // 2. Network drop during sendmessage is classified as unknown (never auto-resend)
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
    ).rejects.toMatchObject({
      outcome: "unknown",
    });

    // 3. HTTP 429 with Retry-After is classified as rate_limited with retryAfterMs
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
    ).rejects.toMatchObject({
      outcome: "rate_limited",
      retryAfterMs: 7000,
    });

    const bodyError = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.error(new Error("body read failed"));
      },
    });
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
            new Response(bodyError, {
              status: 200,
              headers: { "content-type": "application/json" },
            }),
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
            new Response("not-json", {
              status: 200,
              headers: { "content-type": "application/json" },
            }),
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
          fetchImpl: async () => makeJsonResponse({ error: "down" }, 500),
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
          fetchImpl: async () => makeJsonResponse({ ret: -2, errmsg: "no" }),
          checkResolvedIps: false,
        },
        "Hello",
      ),
    ).rejects.toMatchObject({ outcome: "failed" });

    const uploadUrls: string[] = [];
    await expect(
      sendWeixinMediaAttachment(
        {
          baseUrl: "https://ilinkai.weixin.qq.com",
          cdnBaseUrl: "https://novac2c.cdn.weixin.qq.com/c2c",
          allowedApiDomains: ["ilinkai.weixin.qq.com"],
          allowedCdnDomains: ["novac2c.cdn.weixin.qq.com"],
          botToken: "bot_tok",
          toUserId: "wx_u1",
          contextToken: "ctx_valid",
          fetchImpl: async (input) => {
            uploadUrls.push(String(input));
            return makeJsonResponse({ ret: -2, errmsg: "upload refused" });
          },
          checkResolvedIps: false,
        },
        {
          filename: "memo.pdf",
          mimeType: "application/pdf",
          content: Buffer.from("memo", "utf8"),
        },
      ),
    ).rejects.toMatchObject({ outcome: "failed" });
    expect(uploadUrls.some((url) => url.includes("sendmessage"))).toBe(false);

    // 4. CDN upload retry reuses the same filekey, aeskey, and ciphertext
    const getUploadBodies: Array<Record<string, unknown>> = [];
    const cdnPutBodies: Buffer[] = [];
    const sentMessages: Array<Record<string, unknown>> = [];
    let cdnAttempt = 0;

    const mediaFetch: typeof fetch = vi.fn(async (input, init) => {
      const url = String(input);
      if (url.includes("getuploadurl")) {
        getUploadBodies.push(JSON.parse(String(init?.body)));
        return makeJsonResponse({
          ret: 0,
          upload_param: "up_param_1",
        });
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
    const expectedOutboundKeyBase64 = Buffer.from(
      String(getUploadBodies[0]?.aeskey ?? ""),
      "utf8",
    ).toString("base64");
    expect(serializedMsg).toContain(`"aes_key":"${expectedOutboundKeyBase64}"`);
  });

  it("retries /bind after the first I-1 response is lost, and still refuses group and invalid rows", async () => {
    const h = createTestHarness();
    try {
      const bindId = "bot_live_1:bind_lost";
      const seen = new Set<string>();
      vi.mocked(h.backend.registerAndResolveInbound).mockImplementation(
        async (req) => {
          if (req.message_id.endsWith(":group")) {
            return {
              ...emptyInbound(req),
              existing_status: "ignored",
              deny_reason: "weixin_group_unsupported",
            };
          }
          if (req.message_id.endsWith(":invalid")) {
            return {
              ...emptyInbound(req),
              deny_reason: "invalid_inbound_event",
            };
          }
          const first = !seen.has(req.message_id);
          seen.add(req.message_id);
          return {
            ...emptyInbound(req),
            existing_status: first ? null : "ignored",
            deny_reason: "not_linked",
          };
        },
      );

      let bindConsumes = 0;
      vi.mocked(h.backend.consumeBindCode).mockImplementation(async (req) => {
        bindConsumes += 1;
        return {
          ok: true,
          idempotent: bindConsumes > 1,
          user_id: USER_ID,
          platform: "weixin",
          platform_user_id: req.platform_user_id,
          generation: 2,
        };
      });

      const previousClaim = vi
        .mocked(h.backend.authorizeAndClaimDelivery)
        .getMockImplementation();
      if (!previousClaim) {
        throw new Error("authorizeAndClaimDelivery mock is missing");
      }
      let bindOkClaims = 0;
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(
        async (req) => {
          const claimed = await previousClaim(req);
          if (req.delivery_key.includes(":bind_ok")) {
            bindOkClaims += 1;
            if (bindOkClaims > 1) {
              return {
                ...claimed,
                claim_status: "already_completed",
                status: "sent",
              };
            }
          }
          return claimed;
        },
      );

      await h.weixinAdapter.emitInbound(
        weixinDm(bindId, "/bind ABC123", { weixinContextToken: "ctx_bind" }),
      );
      await h.weixinAdapter.emitInbound(
        weixinDm(bindId, "/bind ABC123", { weixinContextToken: "ctx_bind" }),
      );
      const consumesAfterBind = bindConsumes;
      await h.weixinAdapter.emitInbound(
        weixinDm("bot_live_1:group", "/bind ABC123", {
          chatType: "group",
          chatAddress: "weixin:room1",
          chatId: "room1",
          threadScopeKey: "chat:room1",
        }),
      );
      await h.weixinAdapter.emitInbound(
        weixinDm("bot_live_1:invalid", "/bind ABC123"),
      );

      expect(consumesAfterBind).toBe(2);
      expect(bindConsumes).toBe(2);
      expect(
        vi.mocked(h.backend.consumeBindCode).mock.calls.map(([req]) => req.inbound_message_id),
      ).toEqual([bindId, bindId]);
      expect(h.weixinAdapter.sentTexts).toHaveLength(1);
      expect(h.weixinAdapter.sentTexts[0]?.text).toBe(
        "Successfully linked your weixin account to Dalton.",
      );
      expect(h.weixinAdapter.sentTexts[0]?.inboundMessageId).toBe(bindId);
    } finally {
      await h.cleanup();
    }
  });

  it("finishes an in-poll command reply when sendmessage returns -14 and records expired once", async () => {
    const h = createTestHarness();
    try {
      let pollCount = 0;
      const expired = defer<void>();
      vi.mocked(h.backend.getWeixinState).mockResolvedValue(weixinState());
      vi.mocked(h.backend.putWeixinState).mockImplementation(async (req) => {
        if (req.status === "expired") {
          expired.resolve();
        }
        return weixinPut({
          status: req.status ?? "active",
          get_updates_buf: req.get_updates_buf ?? "buf_0",
          updated: true,
        });
      });
      const fakeFetch: typeof fetch = vi.fn(async (input) => {
        const url = String(input);
        if (url.includes("getupdates")) {
          pollCount += 1;
          if (pollCount === 1) {
            return makeJsonResponse({
              ret: 0,
              get_updates_buf: "buf_1",
              msgs: [
                {
                  message_id: "1001",
                  from_user_id: "wx_u1",
                  context_token: "ctx_1",
                  item_list: [{ type: 1, text_item: { text: "/new" } }],
                },
              ],
            });
          }
          return makeJsonResponse({ ret: 0, msgs: [], get_updates_buf: "buf_1" });
        }
        if (url.includes("sendmessage")) {
          return makeJsonResponse({ ret: -14, errcode: -14, errmsg: "session expired" });
        }
        throw new Error(`Unexpected URL: ${url}`);
      });
      const adapter = new WeixinChannelAdapter({
        backend: h.backend,
        baseUrl: "https://ilinkai.weixin.qq.com",
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        fetchImpl: fakeFetch,
        checkResolvedIps: false,
        retryDelayMs: 5,
      });
      adapter.onInboundEvent(async () => {
        await adapter.sendTextChunk({
          targetAddress: "weixin:@me",
          recipientPlatformUserId: "wx_u1",
          text: "Started a new conversation.",
          chunkIndex: 0,
          totalChunks: 1,
          inboundMessageId: "bot_live_1:1001",
        });
      });
      await adapter.start();
      await expired.promise;
      expect(pollCount).toBe(1);
      expect(adapter.getAccountStatus()).toMatchObject({
        status: "expired",
        ilinkBotId: "bot_live_1",
        botGeneration: 5,
      });
      const expiredPuts = vi
        .mocked(h.backend.putWeixinState)
        .mock.calls.filter(([req]) => req.status === "expired");
      expect(expiredPuts).toHaveLength(1);
      expect(expiredPuts[0]?.[0]).toMatchObject({
        expected_ilink_bot_id: "bot_live_1",
        expected_bot_generation: 5,
      });
      await adapter.stop();
    } finally {
      await h.cleanup();
    }
  });

  it("keeps a replacement account active when an older sendmessage returns -14", async () => {
    const h = createTestHarness();
    try {
      const entered = defer<void>();
      const release = defer<void>();
      vi.mocked(h.backend.getWeixinState).mockResolvedValue(
        weixinState({
          ilink_bot_id: "bot_old",
          bot_generation: 1,
          bot_token: "tok_old",
          weixin_context_token: "ctx_old",
          get_updates_buf: "cur_old",
        }),
      );
      vi.mocked(h.backend.putWeixinState).mockImplementation(async (req) =>
        weixinPut({
          ilink_bot_id: req.expected_ilink_bot_id ?? "bot_old",
          status: req.status ?? "active",
          get_updates_buf: req.get_updates_buf ?? "cur_old",
          bot_generation: req.expected_bot_generation ?? 1,
        }),
      );
      const adapter = new WeixinChannelAdapter({
        backend: h.backend,
        baseUrl: "https://ilinkai.weixin.qq.com",
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        checkResolvedIps: false,
        fetchImpl: async (input) => {
          const url = String(input);
          if (!url.includes("sendmessage")) {
            throw new Error(`Unexpected URL: ${url}`);
          }
          entered.resolve();
          await release.promise;
          return makeJsonResponse({ ret: -14, errcode: -14, errmsg: "session expired" });
        },
      });
      await adapter.applyPersistedState(
        weixinState({
          ilink_bot_id: "bot_old",
          bot_generation: 1,
          bot_token: "tok_old",
          get_updates_buf: "cur_old",
        }),
      );
      const sending = adapter.sendTextChunk({
        targetAddress: "weixin:@me",
        recipientPlatformUserId: "wx_u1",
        text: "old reply",
        chunkIndex: 0,
        totalChunks: 1,
        inboundMessageId: "bot_old:1",
        weixinContextToken: "ctx_fallback_unused",
      });
      await entered.promise;
      await adapter.applyPersistedState(
        weixinState({
          ilink_bot_id: "bot_new",
          bot_generation: 2,
          bot_token: "tok_new",
          get_updates_buf: "cur_new",
        }),
      );
      release.resolve();
      await expect(sending).rejects.toMatchObject({ outcome: "failed" });
      expect(adapter.getAccountStatus()).toMatchObject({
        status: "active",
        ilinkBotId: "bot_new",
        botGeneration: 2,
        getUpdatesBuf: "cur_new",
      });
      expect(
        vi.mocked(h.backend.putWeixinState).mock.calls.filter(([req]) => req.status === "expired"),
      ).toHaveLength(0);
    } finally {
      await h.cleanup();
    }
  });

  it("lets the later account generation win when two persisted states overlap", async () => {
    const releases: Array<() => void> = [];
    const firstEntered = defer<void>();
    const secondEntered = defer<void>();
    const adapter = new WeixinChannelAdapter({
      baseUrl: "https://ilinkai.weixin.qq.com",
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: true,
      resolveHostIps: () =>
        new Promise((resolve) => {
          releases.push(() => resolve(["1.1.1.1"]));
          if (releases.length === 1) firstEntered.resolve();
          if (releases.length === 2) secondEntered.resolve();
        }),
    });
    const older = adapter.applyPersistedState(
      weixinState({
        ilink_bot_id: "bot_old",
        bot_generation: 2,
        bot_token: "tok_old",
        get_updates_buf: "cur_old",
      }),
    );
    await firstEntered.promise;
    const newer = adapter.applyPersistedState(
      weixinState({
        ilink_bot_id: "bot_new",
        bot_generation: 4,
        bot_token: "tok_new",
        get_updates_buf: "cur_new",
      }),
    );
    await secondEntered.promise;
    for (const release of releases) release();
    await Promise.all([older, newer]);
    expect(adapter.getAccountStatus()).toMatchObject({
      status: "active",
      ilinkBotId: "bot_new",
      botGeneration: 4,
      getUpdatesBuf: "cur_new",
    });
  });

  it("keeps one poll and the committed cursor when the same generation is applied with an empty cursor", async () => {
    const h = createTestHarness();
    try {
      let pollCount = 0;
      const entered = defer<void>();
      let releaseFirst: ((response: Response) => void) | undefined;
      vi.mocked(h.backend.getWeixinState).mockResolvedValue(
        weixinState({
          ilink_bot_id: "bot_same",
          bot_generation: 4,
          bot_token: "tok_same",
          get_updates_buf: "committed",
        }),
      );
      const adapter = new WeixinChannelAdapter({
        backend: h.backend,
        baseUrl: "https://ilinkai.weixin.qq.com",
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        checkResolvedIps: false,
        retryDelayMs: 5,
        fetchImpl: async (input, init) => {
          const url = String(input);
          if (!url.includes("getupdates")) {
            throw new Error(`Unexpected URL: ${url}`);
          }
          pollCount += 1;
          if (pollCount === 1) {
            entered.resolve();
            return await new Promise<Response>((resolve, reject) => {
              const onAbort = () => reject(new Error("aborted"));
              if (init?.signal?.aborted) {
                onAbort();
                return;
              }
              init?.signal?.addEventListener("abort", onAbort, { once: true });
              releaseFirst = (response) => {
                init?.signal?.removeEventListener("abort", onAbort);
                resolve(response);
              };
            });
          }
          return await new Promise<Response>((_resolve, reject) => {
            const onAbort = () => reject(new Error("aborted"));
            if (init?.signal?.aborted) {
              onAbort();
              return;
            }
            init?.signal?.addEventListener("abort", onAbort, { once: true });
          });
        },
      });
      await adapter.start();
      await entered.promise;
      await adapter.applyPersistedState(
        weixinState({
          ilink_bot_id: "bot_same",
          bot_generation: 4,
          bot_token: "tok_same",
          get_updates_buf: "",
        }),
      );
      expect(pollCount).toBe(1);
      expect(adapter.getAccountStatus().getUpdatesBuf).toBe("committed");
      releaseFirst?.(
        makeJsonResponse({ ret: 0, msgs: [], get_updates_buf: "committed" }),
      );
      await adapter.stop();
      expect(adapter.getAccountStatus().getUpdatesBuf).toBe("committed");
    } finally {
      await h.cleanup();
    }
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

    const aborted = { cancelled: false };
    const parent = new AbortController();
    const pending = safeFetchResponse(
      "https://ilinkai.weixin.qq.com/ilink/bot/getupdates",
      {
        timeoutMs: 10_000,
        signal: parent.signal,
        checkResolvedIps: false,
        allowedDomains: ["ilinkai.weixin.qq.com"],
        fetchImpl: async () => {
          queueMicrotask(() => parent.abort());
          return new Response(stalled(aborted), { status: 200 });
        },
      },
    );
    await expect(pending).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "timeout",
    });
    expect(aborted.cancelled).toBe(true);

    const completed = await safeFetchResponse(
      "https://ilinkai.weixin.qq.com/ilink/bot/getupdates",
      {
        timeoutMs: 1000,
        checkResolvedIps: false,
        allowedDomains: ["ilinkai.weixin.qq.com"],
        fetchImpl: async () =>
          new Response('{"ret":0}', {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      },
    );
    expect(await completed.text()).toBe('{"ret":0}');
  });

  it("passes the current inbound id on /unbind text and file delivery, and does not send with another generation's token", async () => {
    const h = createTestHarness();
    try {
      await h.weixinAdapter.emitInbound(weixinDm("m_unbind", "/unbind"));
      expect(h.weixinAdapter.sentTexts.at(-1)?.text).toBe(
        "Unlinked your weixin account from Dalton.",
      );
      expect(h.weixinAdapter.sentTexts.at(-1)?.inboundMessageId).toBe("m_unbind");

      const delivered = await h.app.deliveryService.executeDelivery({
        deliveryKey: "notice:weixin:m_file:send",
        kind: "notice",
        userId: USER_ID,
        platform: "weixin",
        target: "weixin:@me",
        inboundMessageId: "m_file_in",
        text: "report",
        files: [{ path: "report.pdf", workspace_id: "ws_1" }],
      });
      expect(delivered.status).toBe("sent");
      expect(
        h.weixinAdapter.sentTexts.find((row) => row.inboundMessageId === "m_file_in"),
      ).toMatchObject({ inboundMessageId: "m_file_in" });
      expect(h.weixinAdapter.sentFiles[0]?.inboundMessageId).toBe("m_file_in");

      const urls: string[] = [];
      vi.mocked(h.backend.getWeixinState).mockImplementation(async (includeTokenOrOptions) => {
        const opts =
          includeTokenOrOptions && typeof includeTokenOrOptions === "object"
            ? includeTokenOrOptions
            : undefined;
        const inboundMessageId = opts?.inbound_message_id ?? "";
        return weixinState({
          ilink_bot_id: inboundMessageId ? "bot_old" : "bot_live_1",
          bot_generation: 5,
          weixin_context_token: inboundMessageId ? null : "ctx_live",
        });
      });
      const adapter = new WeixinChannelAdapter({
        backend: h.backend,
        ilinkBotId: "bot_live_1",
        botToken: "tok_current",
        botGeneration: 5,
        baseUrl: "https://ilinkai.weixin.qq.com",
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        checkResolvedIps: false,
        fetchImpl: async (input) => {
          urls.push(String(input));
          return makeJsonResponse({ ret: 0 });
        },
      });
      await expect(
        adapter.sendTextChunk({
          targetAddress: "weixin:@me",
          recipientPlatformUserId: "wx_alice",
          text: "stale",
          chunkIndex: 0,
          totalChunks: 1,
          inboundMessageId: "bot_old:msg",
          weixinContextToken: "ctx_fallback_unused",
        }),
      ).rejects.toMatchObject({ outcome: "failed" });
      expect(urls.some((url) => url.includes("sendmessage"))).toBe(false);
    } finally {
      await h.cleanup();
    }
  });

  it("retries only the cursor commit after registration, and a completed row does not open another model turn", async () => {
    const h = createTestHarness();
    try {
      let pollCount = 0;
      let inboundCount = 0;
      const pollsAtCommit: number[] = [];
      let commitAttempts = 0;
      const secondPoll = defer<void>();
      vi.mocked(h.backend.getWeixinState).mockResolvedValue(weixinState());
      vi.mocked(h.backend.putWeixinState).mockImplementation(async (req) => {
        if (typeof req.get_updates_buf === "string" && req.get_updates_buf.length > 0) {
          commitAttempts += 1;
          pollsAtCommit.push(pollCount);
          const updated = commitAttempts >= 3;
          return weixinPut({
            updated,
            get_updates_buf: updated ? req.get_updates_buf : "buf_0",
          });
        }
        return weixinPut({ status: req.status ?? "active" });
      });
      const adapter = new WeixinChannelAdapter({
        backend: h.backend,
        baseUrl: "https://ilinkai.weixin.qq.com",
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        checkResolvedIps: false,
        retryDelayMs: 5,
        fetchImpl: async (input, init) => {
          const url = String(input);
          if (!url.includes("getupdates")) {
            throw new Error(`Unexpected URL: ${url}`);
          }
          pollCount += 1;
          if (pollCount === 1) {
            return makeJsonResponse({
              ret: 0,
              get_updates_buf: "buf_1",
              msgs: [
                {
                  message_id: "2001",
                  from_user_id: "wx_u1",
                  context_token: "ctx_1",
                  item_list: [{ type: 1, text_item: { text: "hello" } }],
                },
              ],
            });
          }
          if (pollCount === 2) {
            secondPoll.resolve();
            return makeJsonResponse({ ret: 0, msgs: [], get_updates_buf: "buf_1" });
          }
          return await new Promise<Response>((_resolve, reject) => {
            const onAbort = () => reject(new Error("aborted"));
            if (init?.signal?.aborted) {
              onAbort();
              return;
            }
            init?.signal?.addEventListener("abort", onAbort, { once: true });
          });
        },
      });
      adapter.onInboundEvent(async () => {
        inboundCount += 1;
      });
      await adapter.start();
      await secondPoll.promise;
      expect(inboundCount).toBe(1);
      expect(commitAttempts).toBe(3);
      expect(pollsAtCommit).toEqual([1, 1, 1]);
      await adapter.stop();

      vi.mocked(h.backend.registerAndResolveInbound).mockImplementation(async (req) => ({
        ...emptyInbound(req),
        existing_status: "completed",
        linked: true,
        allowed: true,
        dalton_user_id: USER_ID,
        fallback_pending: false,
        thread_id: "thread_done",
        run_id: "run_done",
      }));
      await h.weixinAdapter.emitInbound(weixinDm("m_done", "hello again"));
      expect(h.backend.createThreadMessages).not.toHaveBeenCalled();
    } finally {
      await h.cleanup();
    }
  });

  it("strips unsupported Markdown syntax cleanly via filterWeixinMarkdown", () => {
    const md =
      "##### Heading 5\n\nHere is *中文斜体* and ![chart](https://example.com/a.png) plus **bold**.";
    const filtered = filterWeixinMarkdown(md);
    expect(filtered).toContain("Heading 5");
    expect(filtered).toContain("中文斜体");
    expect(filtered).not.toContain("*中文斜体*");
    expect(filtered).not.toContain("![chart]");
    expect(filtered).toContain("**bold**");
  });
});
