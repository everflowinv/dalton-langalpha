import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  I1RegisterAndResolveResponse,
  I2BindCodeConsumeResponse,
} from "../src/backend-client.js";
import { ChannelDeliveryError } from "../src/channels/adapter.js";
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

  it("keeps a stored command effect when confirmation delivery fails and does not run the command again", async () => {
    const effects = new Map<string, Record<string, unknown>>();
    const defaultRegister = vi.mocked(h.backend.registerAndResolveInbound).getMockImplementation()!;
    vi.mocked(h.backend.registerAndResolveInbound).mockImplementation(async (req) => {
      const base = await defaultRegister(req);
      const stored = effects.get(`${req.platform}:${req.message_id}`);
      return stored
        ? { ...base, raw_payload: { ...(base.raw_payload ?? {}), ...stored } }
        : base;
    });
    const defaultUpdate = vi.mocked(h.backend.updateInboundProgress).getMockImplementation()!;
    vi.mocked(h.backend.updateInboundProgress).mockImplementation(async (req) => {
      if (req.raw_payload_patch && req.platform && req.message_id) {
        const key = `${req.platform}:${req.message_id}`;
        effects.set(key, { ...(effects.get(key) ?? {}), ...req.raw_payload_patch });
      }
      return defaultUpdate(req);
    });

    const bindEvent = {
      platform: "feishu" as const,
      platformUserId: "ou_alice",
      chatType: "dm" as const,
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_bind_retry",
      text: "/bind ABC123",
    };
    let sends = 0;
    h.feishuAdapter.setSendTextBehavior(() => {
      sends += 1;
      throw new Error("confirmation socket hang up");
    });
    await h.feishuAdapter.emitInbound(bindEvent);
    await h.feishuAdapter.emitInbound(bindEvent);
    expect(vi.mocked(h.backend.consumeBindCode).mock.calls).toHaveLength(1);
    expect(sends).toBe(1);
    expect(effects.get("feishu:m_bind_retry")?.discord_command_effect).toMatchObject({
      cmd: "bind",
      applied: true,
      reply_delivered: false,
    });

    const workspaceEvent = {
      ...bindEvent,
      messageId: "m_ws_retry",
      text: "/workspace quant",
    };
    const sessionCallsBefore = vi.mocked(h.backend.sessionCommand).mock.calls.length;
    await h.feishuAdapter.emitInbound(workspaceEvent);
    await h.feishuAdapter.emitInbound(workspaceEvent);
    const workspaceCalls = vi.mocked(h.backend.sessionCommand).mock.calls
      .slice(sessionCallsBefore)
      .filter(([req]) => req.cmd === "workspace_switch");
    expect(workspaceCalls).toHaveLength(1);
    expect(sends).toBe(2);

    h.feishuAdapter.setSendTextBehavior(() => {
      throw new ChannelDeliveryError("forbidden", {
        outcome: "failed",
        code: "not_allowed",
      });
    });
    vi.mocked(h.backend.consumeBindCode).mockResolvedValueOnce({
      ok: false,
      reason: "revoked_in_group",
    });
    const groupBind = {
      ...bindEvent,
      chatType: "group" as const,
      chatAddress: "feishu:oc_group1",
      chatId: "oc_group1",
      threadScopeKey: "chat:oc_group1",
      messageId: "m_bind_group_retry",
      text: "/bind ABC123",
      isGroupTrigger: true,
    };
    const consumeBeforeGroup = vi.mocked(h.backend.consumeBindCode).mock.calls.length;
    await h.feishuAdapter.emitInbound(groupBind);
    await h.feishuAdapter.emitInbound(groupBind);
    expect(vi.mocked(h.backend.consumeBindCode).mock.calls.length).toBe(consumeBeforeGroup + 1);
    expect(effects.get("feishu:m_bind_group_retry")?.discord_command_effect).toMatchObject({
      cmd: "bind",
      applied: true,
      reply_delivered: false,
      terminal_status: "failed",
    });

    h.feishuAdapter.setSendTextBehavior(null);
    const groupTextsBefore = h.feishuAdapter.sentTexts.length;
    await h.feishuAdapter.emitInbound(groupBind);
    expect(h.feishuAdapter.sentTexts.length).toBe(groupTextsBefore + 1);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain("revoked");
    expect(vi.mocked(h.backend.consumeBindCode).mock.calls.length).toBe(consumeBeforeGroup + 1);
    expect(effects.get("feishu:m_bind_group_retry")?.discord_command_effect).toMatchObject({
      reply_delivered: true,
    });
    await h.feishuAdapter.emitInbound(groupBind);
    expect(h.feishuAdapter.sentTexts.length).toBe(groupTextsBefore + 1);
    expect(vi.mocked(h.backend.consumeBindCode).mock.calls.length).toBe(consumeBeforeGroup + 1);

    vi.mocked(h.backend.cancelThreadTurn).mockResolvedValueOnce({ status: 503 });
    const stopEvent = {
      ...bindEvent,
      messageId: "m_stop_retry",
      text: "/stop",
    };
    await expect(h.feishuAdapter.emitInbound(stopEvent)).rejects.toThrow(/not confirmed/i);
    expect(effects.has("feishu:m_stop_retry")).toBe(false);
    expect(vi.mocked(h.backend.cancelThreadTurn).mock.calls).toHaveLength(1);
    await h.feishuAdapter.emitInbound(stopEvent);
    const sentAfterStop = h.feishuAdapter.sentTexts.length;
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain("Stopped the current run.");
    await h.feishuAdapter.emitInbound(stopEvent);
    expect(h.feishuAdapter.sentTexts.length).toBe(sentAfterStop);
    expect(vi.mocked(h.backend.cancelThreadTurn).mock.calls).toHaveLength(2);
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
