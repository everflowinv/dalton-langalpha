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
} from "../src/shared/safe-fetch.js";
import { MAX_ATTACHMENT_BYTES } from "../src/types.js";
import { createTestHarness, USER_ID } from "./helpers.js";

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
      expect(activatedStates).toHaveLength(1);

      // Duplicate wait on Admin 2's confirmed session is idempotent (no second activation)
      const wait2Dup = await qrManager.waitQrLogin(
        "admin_2",
        start2.session_key,
      );
      expect(wait2Dup.status).toBe("confirmed");
      expect(activatedStates).toHaveLength(1);

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
