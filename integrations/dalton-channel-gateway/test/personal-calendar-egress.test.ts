import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createTestHarness,
  RUN_ID,
  type TestHarness,
  THREAD_ID,
  USER_ID,
} from "./helpers.js";

const REDIRECT_NOTICE = "本次结果含个人日历内容，已发送到你的私聊";

describe("Personal integration channel egress guard (automation, fallback & retry)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("Feishu group automation on personal-private thread redirects real content to DM and sends only sanitized notice to group", async () => {
    const execId = "33333333-4444-5555-6666-777777777771";
    const secretBody = "【明日私人日程】10:00 体检预约，15:00 律师一对一沟通";

    vi.mocked(h.backend.finishAutomationRun).mockResolvedValueOnce({
      status: 200,
      data: {
        execution_id: execId,
        status: "finishing",
        previous_status: "registered",
        user_id: USER_ID,
        workspace_id: null,
        thread_id: THREAD_ID,
        run_id: null,
        automation_name: "每日日程播报",
        finish_status: "completed",
        final_text: secretBody,
        has_personal_integration_data: true,
        targets: [
          {
            entry: "feishu:oc_group_alpha",
            address: "feishu:oc_group_alpha",
            name: "投资研究群",
            ok: true,
            message: null,
          },
        ],
      },
    });

    // Simulate backend I-6 forcing sanitized text on `:personal_redirect_notice`
    // and authorizing the owner's `feishu:@me` DM.
    const defaultClaim = vi.mocked(h.backend.authorizeAndClaimDelivery).getMockImplementation();
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(async (req) => {
      if (req.delivery_key.endsWith(":personal_redirect_notice")) {
        return {
          claim_status: "claimed",
          platform: "feishu",
          target_address: req.target ?? "feishu:oc_group_alpha",
          is_current: true,
          personal_private_redirect_notice: true,
          forced_text: REDIRECT_NOTICE,
        };
      }
      if (defaultClaim) {
        return defaultClaim(req);
      }
      return {
        claim_status: "claimed",
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_platform_user_id: "ou_alice",
        dm_recipient_platform_user_id: "ou_alice",
        is_current: true,
      };
    });

    const finishRes = await h.app.agentRoutes.handleFinishAutomationRun(
      USER_ID,
      execId,
      {
        status: "completed",
        final_text: secretBody,
        has_personal_integration_data: true,
      },
    );

    expect(finishRes.status).toBe(200);
    expect(finishRes.data.targets).toHaveLength(1);
    expect(finishRes.data.targets?.[0]?.reached).toBe(true);
    expect(h.backend.deliveryClaims.some((c) => c.target === "feishu:@me")).toBe(true);
    expect(
      h.backend.deliveryClaims.some(
        (c) =>
          c.target === "feishu:oc_group_alpha" &&
          c.delivery_key.endsWith(":personal_redirect_notice"),
      ),
    ).toBe(true);

    // Verify Feishu adapter deliveries: DM got full content; group got ONLY redirect notice!
    const dmMessages = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === "feishu:@me",
    );
    const groupMessages = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === "feishu:oc_group_alpha",
    );
    expect(dmMessages.length).toBeGreaterThanOrEqual(1);
    expect(dmMessages.some((m) => m.text.includes("体检预约"))).toBe(true);

    expect(groupMessages).toHaveLength(1);
    expect(groupMessages[0]?.text).toBe(REDIRECT_NOTICE);
    expect(groupMessages[0]?.text).not.toContain("体检预约");
    expect(groupMessages[0]?.text).not.toContain("律师");
    expect(h.feishuAdapter.sentFiles).toHaveLength(0);
  });

  it("Discord channel automation on personal-private thread redirects real content to Discord DM and sends only sanitized notice to channel", async () => {
    const execId = "33333333-4444-5555-6666-777777777772";
    const secretBody = "【个人日程提醒】14:00 董事会私密议程";

    vi.mocked(h.backend.finishAutomationRun).mockResolvedValueOnce({
      status: 200,
      data: {
        execution_id: execId,
        status: "finishing",
        previous_status: "registered",
        user_id: USER_ID,
        workspace_id: null,
        thread_id: THREAD_ID,
        run_id: null,
        automation_name: "Discord 日程速报",
        finish_status: "completed",
        final_text: secretBody,
        has_personal_integration_data: true,
        targets: [
          {
            entry: "discord:guild-1/chan-1",
            address: "discord:guild-1/chan-1",
            name: "#research",
            ok: true,
            message: null,
          },
        ],
      },
    });

    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(async (req) => {
      if (req.delivery_key.endsWith(":personal_redirect_notice")) {
        return {
          claim_status: "claimed",
          platform: "discord",
          target_address: "discord:guild-1/chan-1",
          is_current: true,
          personal_private_redirect_notice: true,
          forced_text: REDIRECT_NOTICE,
        };
      }
      return {
        claim_status: "claimed",
        platform: "discord",
        target_address: "discord:@me",
        is_current: true,
      };
    });

    const finishRes = await h.app.agentRoutes.handleFinishAutomationRun(
      USER_ID,
      execId,
      {
        status: "completed",
        final_text: secretBody,
        has_personal_integration_data: true,
      },
    );

    expect(finishRes.status).toBe(200);
    expect(finishRes.data.targets).toHaveLength(1);
    expect(finishRes.data.targets?.[0]?.reached).toBe(true);
    expect(h.backend.deliveryClaims.some((c) => c.target === "discord:@me")).toBe(true);
    expect(
      h.backend.deliveryClaims.some(
        (c) =>
          c.target === "discord:guild-1/chan-1" &&
          c.delivery_key.endsWith(":personal_redirect_notice"),
      ),
    ).toBe(true);

    const dmMessages = h.discordAdapter.sentTexts.filter(
      (m) => m.targetAddress === "discord:@me",
    );
    const channelMessages = h.discordAdapter.sentTexts.filter(
      (m) => m.targetAddress === "discord:guild-1/chan-1",
    );
    expect(dmMessages.length).toBeGreaterThanOrEqual(1);
    expect(dmMessages.some((m) => m.text.includes("董事会私密议程"))).toBe(true);

    expect(channelMessages).toHaveLength(1);
    expect(channelMessages[0]?.text).toBe(REDIRECT_NOTICE);
    expect(channelMessages[0]?.text).not.toContain("董事会私密议程");
    expect(h.discordAdapter.sentFiles).toHaveLength(0);
  });

  it("DeliveryService enforces backend forced_text and strips files on personal_private_redirect_notice", async () => {
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
      claim_status: "claimed",
      platform: "feishu",
      target_address: "feishu:oc_group_alpha",
      is_current: true,
      personal_private_redirect_notice: true,
      forced_text: REDIRECT_NOTICE,
    });

    const res = await h.app.deliveryService.executeDelivery({
      deliveryKey: `auto:${RUN_ID}:feishu:oc_group_alpha:personal_redirect_notice`,
      kind: "automation",
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      platform: "feishu",
      target: "feishu:oc_group_alpha",
      text: "LEAKED CALENDAR EVENT SHOULD NEVER BE SENT",
      files: [{ path: "secret_calendar.pdf", workspace_id: "ws-1" }],
    });

    expect(res.status).toBe("sent");
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(REDIRECT_NOTICE);
    expect(h.feishuAdapter.sentFiles).toHaveLength(0);
  });

  it("Backend I-6 personal_integration_private_only denial redirects fallback/retry to verified DM and posts sanitized notice to group", async () => {
    const execId = "33333333-4444-5555-6666-777777777773";
    const secretBody = "【补投日程】16:00 医疗复诊";

    // Even if `has_personal_integration_data` was NOT pre-flagged on the automation row,
    // backend I-6 enforces privacy dynamically and denies the group target with `personal_integration_private_only`.
    vi.mocked(h.backend.finishAutomationRun).mockResolvedValueOnce({
      status: 200,
      data: {
        execution_id: execId,
        status: "finishing",
        previous_status: "registered",
        user_id: USER_ID,
        workspace_id: null,
        thread_id: THREAD_ID,
        run_id: null,
        automation_name: "延迟触发检查",
        finish_status: "completed",
        final_text: secretBody,
        has_personal_integration_data: false,
        targets: [
          {
            entry: "feishu:oc_group_beta",
            address: "feishu:oc_group_beta",
            name: "项目群",
            ok: true,
            message: null,
          },
        ],
      },
    });

    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(async (req) => {
      if (
        req.target === "feishu:oc_group_beta" &&
        !req.delivery_key.endsWith(":personal_redirect_notice")
      ) {
        return {
          claim_status: "denied",
          status: "failed",
          code: "personal_integration_private_only",
          has_personal_integration_data: true,
          redirect_dm_address: "feishu:@me",
          message: "personal calendar thread cannot send content to group",
        };
      }
      if (req.delivery_key.endsWith(":personal_redirect_notice")) {
        return {
          claim_status: "claimed",
          platform: "feishu",
          target_address: "feishu:oc_group_beta",
          is_current: true,
          personal_private_redirect_notice: true,
          forced_text: REDIRECT_NOTICE,
        };
      }
      return {
        claim_status: "claimed",
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_platform_user_id: "ou_alice",
        dm_recipient_platform_user_id: "ou_alice",
        is_current: true,
      };
    });

    const finishRes = await h.app.agentRoutes.handleFinishAutomationRun(
      USER_ID,
      execId,
      {
        status: "completed",
        final_text: secretBody,
      },
    );

    expect(finishRes.status).toBe(200);
    expect(finishRes.data.targets?.[0]?.reached).toBe(true);
    expect(h.backend.deliveryClaims.some((c) => c.target === "feishu:@me")).toBe(true);

    const dmMessages = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === "feishu:@me",
    );
    const groupMessages = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === "feishu:oc_group_beta",
    );
    expect(dmMessages.some((m) => m.text.includes("医疗复诊"))).toBe(true);
    expect(groupMessages).toHaveLength(1);
    expect(groupMessages[0]?.text).toBe(REDIRECT_NOTICE);
    expect(groupMessages[0]?.text).not.toContain("医疗复诊");
  });

  it("delivers personal_integration_notice once per thread in verified 1:1 DM (including on cancelled approval) and never in group", async () => {
    const firstTouchMsg =
      "提示：本对话已接入你的个人日历私密数据，仅限创建者本人访问；后续若断开该连接，将从本轮起向后清除同线程产生的后续对话内容，纯研究讨论建议另开新线程。";
    const dmThreadId = "44444444-5555-6666-7777-888888888881";

    // Simulate a Feishu 1:1 DM turn that emits personal_integration_notice twice (e.g. live + replay)
    // and then hits a write approval interrupt (even if the user later cancels the approval).
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      isSse: true,
      status: 200,
      events: (async function* () {
        yield {
          event: "metadata",
          data: JSON.stringify({
            thread_id: dmThreadId,
            run_id: RUN_ID,
            turn_index: 0,
          }),
        };
        yield {
          event: "personal_integration_notice",
          data: JSON.stringify({
            thread_id: dmThreadId,
            agent: "main",
            service: "calendar",
            provider: "google",
            turn_index: 0,
            message: firstTouchMsg,
          }),
        };
        yield {
          event: "personal_integration_notice",
          data: JSON.stringify({
            thread_id: dmThreadId,
            agent: "main",
            service: "calendar",
            provider: "google",
            turn_index: 0,
            message: firstTouchMsg,
          }),
        };
        yield {
          event: "interrupt",
          data: JSON.stringify({
            thread_id: dmThreadId,
            run_id: RUN_ID,
            interrupt_id: "int-cal-cancel",
            action_requests: [
              {
                type: "dalton_calendar_update_event",
                name: "dalton_calendar",
                description: "确认修改日程",
                dm_notice: "请前往网页端确认修改日程",
              },
            ],
          }),
        };
      })(),
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      messageId: "msg-dm-first-touch-1",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatId: "oc_dm_alice",
      chatAddress: "feishu:@me",
      threadScopeKey: "dm:ou_alice",
      text: "帮我改一下明天的日程",
    });

    const noticeClaims = h.backend.deliveryClaims.filter(
      (c) => c.delivery_key === `notice:personal_first_touch:${dmThreadId}`,
    );
    expect(noticeClaims).toHaveLength(1);
    expect(noticeClaims[0]?.target).toBe("feishu:@me");

    const dmNotices = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === "feishu:@me" && m.text === firstTouchMsg,
    );
    expect(dmNotices).toHaveLength(1);

    // Group chat must NEVER receive personal_integration_notice
    const groupThreadId = "44444444-5555-6666-7777-888888888882";
    const delivered = await (
      h.app.orchestrator as unknown as {
        deliverPersonalFirstTouchNotice: (p: Record<string, unknown>) => Promise<boolean>;
      }
    ).deliverPersonalFirstTouchNotice({
      userId: USER_ID,
      threadId: groupThreadId,
      runId: RUN_ID,
      platform: "feishu",
      chatType: "group",
      targetAddress: "feishu:oc_group_alpha",
      message: firstTouchMsg,
    });
    expect(delivered).toBe(false);
    expect(
      h.backend.deliveryClaims.some(
        (c) => c.delivery_key === `notice:personal_first_touch:${groupThreadId}`,
      ),
    ).toBe(false);
  });

  it("private thread attachment fallback redirects files with explanation to DM and posts only sanitized notice to group", async () => {
    const runId = "55555555-6666-7777-8888-999999999991";
    const groupTarget = "feishu:oc_group_attach";

    vi.mocked(h.backend.downloadWorkspaceFile).mockResolvedValueOnce({
      content: Buffer.from("calendar summary", "utf8"),
      mimeType: "text/plain",
    });

    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(async (req) => {
      if (
        req.target === groupTarget &&
        !req.delivery_key.endsWith(":personal_redirect_notice")
      ) {
        return {
          claim_status: "denied",
          status: "failed",
          code: "personal_integration_private_only",
          has_personal_integration_data: true,
          redirect_dm_address: "feishu:@me",
          message: "personal calendar thread cannot send attachments to group",
        };
      }
      if (req.delivery_key.endsWith(":personal_redirect_notice")) {
        return {
          claim_status: "claimed",
          platform: "feishu",
          target_address: groupTarget,
          is_current: true,
          personal_private_redirect_notice: true,
          forced_text: REDIRECT_NOTICE,
        };
      }
      return {
        claim_status: "claimed",
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_platform_user_id: "ou_alice",
        dm_recipient_platform_user_id: "ou_alice",
        is_current: true,
      };
    });

    const redirectFn = (
      h.app.orchestrator as unknown as {
        executeDeliverableFallbackWithPersonalRedirect: (
          p: Record<string, unknown>,
        ) => Promise<{ status: string } | null>;
      }
    ).executeDeliverableFallbackWithPersonalRedirect.bind(h.app.orchestrator);

    const res = await redirectFn({
      deliveryKey: `deliverables:${runId}`,
      userId: USER_ID,
      threadId: THREAD_ID,
      workspaceId: "ws-1",
      runId,
      platform: "feishu",
      targetAddress: groupTarget,
      inboundMessageId: null,
      files: [{ path: "calendar_notes.txt", workspace_id: "ws-1" }],
      linkOnlyFiles: [],
      threadScopeKey: null,
      sourceScopeKey: null,
      sourceGeneration: null,
      activityOrigin: "automation",
    });
    expect(res?.status).toBe("sent");

    // DM receives both the attachment and the explanation text
    const dmTexts = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === "feishu:@me",
    );
    const dmFiles = h.feishuAdapter.sentFiles.filter(
      (f) => f.targetAddress === "feishu:@me",
    );
    expect(dmTexts.some((m) => m.text.includes("个人日历相关附件"))).toBe(true);
    expect(dmFiles).toHaveLength(1);
    expect(dmFiles[0]?.filename).toBe("calendar_notes.txt");

    // Group receives ONLY the sanitized redirect notice and ZERO attachments
    const groupTexts = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === groupTarget,
    );
    const groupFiles = h.feishuAdapter.sentFiles.filter(
      (f) => f.targetAddress === groupTarget,
    );
    expect(groupTexts).toHaveLength(1);
    expect(groupTexts[0]?.text).toBe(REDIRECT_NOTICE);
    expect(groupFiles).toHaveLength(0);
  });

  it("private thread attachment DM redirect does not report partial or notify group when all attachments fail", async () => {
    const runId = "55555555-6666-7777-8888-999999999992";
    const groupTarget = "feishu:oc_group_attach_fail";

    // Simulate every attachment download failing (no file uploaded, no link fallback delivered)
    vi.mocked(h.backend.downloadWorkspaceFile).mockRejectedValue(
      new Error("workspace storage unreachable"),
    );

    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(async (req) => {
      if (
        req.target === groupTarget &&
        !req.delivery_key.endsWith(":personal_redirect_notice")
      ) {
        return {
          claim_status: "denied",
          status: "failed",
          code: "personal_integration_private_only",
          has_personal_integration_data: true,
          redirect_dm_address: "feishu:@me",
          message: "personal calendar thread cannot send attachments to group",
        };
      }
      if (req.delivery_key.endsWith(":personal_redirect_notice")) {
        return {
          claim_status: "claimed",
          platform: "feishu",
          target_address: groupTarget,
          is_current: true,
          personal_private_redirect_notice: true,
          forced_text: REDIRECT_NOTICE,
        };
      }
      return {
        claim_status: "claimed",
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_platform_user_id: "ou_alice",
        dm_recipient_platform_user_id: "ou_alice",
        is_current: true,
      };
    });

    const redirectFn = (
      h.app.orchestrator as unknown as {
        executeDeliverableFallbackWithPersonalRedirect: (
          p: Record<string, unknown>,
        ) => Promise<{ status: string } | null>;
      }
    ).executeDeliverableFallbackWithPersonalRedirect.bind(h.app.orchestrator);

    const res = await redirectFn({
      deliveryKey: `deliverables:${runId}`,
      userId: USER_ID,
      threadId: THREAD_ID,
      workspaceId: "ws-1",
      runId,
      platform: "feishu",
      targetAddress: groupTarget,
      inboundMessageId: null,
      files: [{ path: "calendar_notes.txt", workspace_id: "ws-1" }],
      linkOnlyFiles: [],
      threadScopeKey: null,
      sourceScopeKey: null,
      sourceGeneration: null,
      activityOrigin: "automation",
    });

    // Orchestrator MUST NOT report settled/partial when all attachments failed
    expect(res).toBeNull();

    // Persisted delivery progress for the DM attempt MUST NOT be marked `partial` or `sent`
    const dmProgress = h.backend.deliveryUpdates.filter(
      (p) => p.delivery_key === `deliverables:${runId}:personal_dm`,
    );
    const finalDmProgress = dmProgress[dmProgress.length - 1];
    expect(finalDmProgress).toBeDefined();
    expect(finalDmProgress?.status).not.toBe("partial");
    expect(finalDmProgress?.status).not.toBe("sent");
    expect(["failed", "unknown"]).toContain(finalDmProgress?.status);

    // Group MUST NOT receive "已发送到你的私聊" notice when no attachment actually succeeded
    const groupTexts = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === groupTarget,
    );
    expect(groupTexts).toHaveLength(0);
    expect(
      h.backend.deliveryClaims.some(
        (c) => c.delivery_key === `fallback:${runId}:personal_redirect_notice`,
      ),
    ).toBe(false);

    // DM MUST NOT receive premature "已发送到你的私聊" notice when all attachments & fallback links failed
    const dmTexts = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === "feishu:@me",
    );
    expect(dmTexts).toHaveLength(0);
    expect(dmTexts.some((m) => m.text.includes("已发送到你的私聊"))).toBe(false);
    expect(
      h.backend.deliveryClaims.some(
        (c) => c.delivery_key === `deliverables:${runId}:personal_dm_notice`,
      ),
    ).toBe(false);
  });

  it("private thread attachment DM redirect sends confirmation to DM only after attachment or fallback link succeeds", async () => {
    const runId = "55555555-6666-7777-8888-999999999993";
    const groupTarget = "feishu:oc_group_attach_link";

    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(async (req) => {
      if (
        req.target === groupTarget &&
        !req.delivery_key.endsWith(":personal_redirect_notice")
      ) {
        return {
          claim_status: "denied",
          status: "failed",
          code: "personal_integration_private_only",
          has_personal_integration_data: true,
          redirect_dm_address: "feishu:@me",
          message: "personal calendar thread cannot send attachments to group",
        };
      }
      if (req.delivery_key.endsWith(":personal_redirect_notice")) {
        return {
          claim_status: "claimed",
          platform: "feishu",
          target_address: groupTarget,
          is_current: true,
          personal_private_redirect_notice: true,
          forced_text: REDIRECT_NOTICE,
        };
      }
      return {
        claim_status: "claimed",
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_platform_user_id: "ou_alice",
        dm_recipient_platform_user_id: "ou_alice",
        is_current: true,
      };
    });

    const redirectFn = (
      h.app.orchestrator as unknown as {
        executeDeliverableFallbackWithPersonalRedirect: (
          p: Record<string, unknown>,
        ) => Promise<{ status: string } | null>;
      }
    ).executeDeliverableFallbackWithPersonalRedirect.bind(h.app.orchestrator);

    const res = await redirectFn({
      deliveryKey: `deliverables:${runId}`,
      userId: USER_ID,
      threadId: THREAD_ID,
      workspaceId: "ws-1",
      runId,
      platform: "feishu",
      targetAddress: groupTarget,
      inboundMessageId: null,
      files: [],
      linkOnlyFiles: [
        {
          path: "calendar_large_export.pdf",
          workspace_id: "ws-1",
          filename: "calendar_large_export.pdf",
          reason: "oversized",
        },
      ],
      threadScopeKey: null,
      sourceScopeKey: null,
      sourceGeneration: null,
      activityOrigin: "automation",
    });

    expect(res?.status).toBe("sent");
    const dmTexts = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === "feishu:@me",
    );
    // Link is delivered first, followed by the post-delivery confirmation text
    expect(dmTexts.length).toBeGreaterThanOrEqual(2);
    expect(dmTexts[dmTexts.length - 1]?.text).toBe(
      "本次结果含个人日历相关附件，已发送到你的私聊",
    );
    const groupTexts = h.feishuAdapter.sentTexts.filter(
      (m) => m.targetAddress === groupTarget,
    );
    expect(groupTexts).toHaveLength(1);
    expect(groupTexts[0]?.text).toBe(REDIRECT_NOTICE);
  });
});
