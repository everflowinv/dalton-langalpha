import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  I1RegisterAndResolveResponse,
  I2BindCodeConsumeResponse,
} from "../src/backend-client.js";
import { makeInboundRequestKey } from "../src/uuid.js";
import {
  createTestHarness,
  HOME_WS_ID,
  loadFixtureKey,
  RUN_ID,
  type TestHarness,
  THREAD_ID,
  USER_ID,
  WORKSPACE_ID,
} from "./helpers.js";

describe("Slash Commands, Attachment Upload (`uploads/...`), Multimodal Images & Uninitialized Home (Codex #4/#5/#6)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("handles /bind (group leak revoke + DM success), /unbind, /workspace, /new, and /stop", async () => {
    // /bind in group -> revoked_in_group
    vi.mocked(h.backend.consumeBindCode).mockResolvedValueOnce(
      loadFixtureKey<I2BindCodeConsumeResponse>(
        "i2_bind_code_consume.json",
        "bind_revoked_in_group",
      ),
    );
    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "group",
      chatAddress: "feishu:oc_group1",
      chatId: "oc_group1",
      threadScopeKey: "chat:oc_group1",
      messageId: "m_bind_grp",
      text: "/bind ABC123",
      isGroupTrigger: true,
    });
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain("revoked");

    // /bind in DM -> success
    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_bind_dm",
      text: "/bind ABC123",
    });
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      "Successfully linked",
    );

    // /workspace (list)
    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_ws_list",
      text: "/workspace",
    });
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain("Quant Workspace");

    // /new and /stop
    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_new",
      text: "/new",
    });
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      "Started a new conversation",
    );

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_stop",
      text: "/stop",
    });
    expect(h.backend.cancelThreadTurn).toHaveBeenCalledWith(
      USER_ID,
      THREAD_ID,
      RUN_ID,
    );
  });

  it("uploads attachments to `uploads/{platform}/{message_id}/{filename}` (never `.agents/`) and formats inline images with `{ type: \"text\" }` first followed by `{ type: \"image\", image_url: \"data:...\" }` (Codex #4/#5, Grok #13)", async () => {
    const pngBytes = Buffer.from([
      0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
    ]);
    h.feishuAdapter.setAttachment("att_img_1", {
      content: pngBytes,
      filename: "chart.png",
      mimeType: "image/png",
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_with_img",
      text: "Analyze this chart",
      attachments: [
        {
          id: "att_img_1",
          filename: "chart.png",
          mimeType: "image/png",
          sizeBytes: pngBytes.byteLength,
        },
      ],
    });

    expect(h.backend.uploadWorkspaceFile).toHaveBeenCalledWith(
      USER_ID,
      WORKSPACE_ID,
      "uploads/feishu/m_with_img/chart.png",
      "chart.png",
      pngBytes,
      "image/png",
    );

    const sentContent = h.backend.appendCalls[0]?.payload.messages[0]?.content;
    expect(Array.isArray(sentContent)).toBe(true);
    if (Array.isArray(sentContent)) {
      expect(sentContent[0]).toEqual({
        type: "text",
        text: "Analyze this chart\n\n[Attached file in workspace: uploads/feishu/m_with_img/chart.png]",
      });
      expect(sentContent[1]).toEqual({
        type: "image",
        image_url: `data:image/png;base64,${pngBytes.toString("base64")}`,
      });
    }
  });

  it("initializes lazy Home workspace (`workspace_status === null`) via `ensureHomeWorkspace` before `startWorkspace`, and refuses starting stopped colleague workspaces (Codex #6)", async () => {
    const pdfBytes = Buffer.from("%PDF-1.7 test", "utf8");
    h.feishuAdapter.setAttachment("att_pdf_1", {
      content: pdfBytes,
      filename: "brief.pdf",
      mimeType: "application/pdf",
    });

    // 1. Uninitialized Home workspace (`workspace_id: HOME_WS_ID, workspace_status: null`)
    const lazyHomeFixture = loadFixtureKey<I1RegisterAndResolveResponse>(
      "i1_register_and_resolve.json",
      "lazy_home",
    );
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...lazyHomeFixture,
      request_key: makeInboundRequestKey("feishu", "m_lazy_home"),
    });

    vi.mocked(h.backend.ensureHomeWorkspace).mockResolvedValueOnce({
      workspace_id: HOME_WS_ID,
      status: "stopped",
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_lazy_home",
      text: "Upload to lazy Home",
      attachments: [
        {
          id: "att_pdf_1",
          filename: "brief.pdf",
          mimeType: "application/pdf",
          sizeBytes: pdfBytes.byteLength,
        },
      ],
    });

    expect(h.backend.ensureHomeWorkspace).toHaveBeenCalledWith(USER_ID);
    expect(h.backend.startWorkspace).toHaveBeenCalledWith(USER_ID, HOME_WS_ID);
    expect(h.backend.uploadWorkspaceFile).toHaveBeenCalledWith(
      USER_ID,
      HOME_WS_ID,
      "uploads/feishu/m_lazy_home/brief.pdf",
      "brief.pdf",
      pdfBytes,
      "application/pdf",
    );

    // 2. Colleague's workspace is stopped -> refuses to start colleague's sandbox
    vi.mocked(h.backend.startWorkspace).mockClear();
    const colleagueStoppedFixture = loadFixtureKey<I1RegisterAndResolveResponse>(
      "i1_register_and_resolve.json",
      "colleague_stopped",
    );
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...colleagueStoppedFixture,
      request_key: makeInboundRequestKey("feishu", "m_colleague_stopped"),
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "group",
      chatAddress: "feishu:oc_group1",
      chatId: "oc_group1",
      threadScopeKey: "chat:oc_group1",
      messageId: "m_colleague_stopped",
      text: "Upload to colleague ws",
      isGroupTrigger: true,
      attachments: [
        {
          id: "att_pdf_1",
          filename: "brief.pdf",
          mimeType: "application/pdf",
          sizeBytes: pdfBytes.byteLength,
        },
      ],
    });

    expect(h.backend.startWorkspace).not.toHaveBeenCalled();
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      "shared colleague workspace is currently stopped",
    );
  });
});
