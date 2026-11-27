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
});
