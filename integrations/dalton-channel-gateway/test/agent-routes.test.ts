import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChannelDeliveryError } from "../src/channels/adapter.js";
import { DeliveryService } from "../src/delivery-service.js";
import { MAX_AUTOMATION_FINAL_TEXT_CHARS } from "../src/types.js";
import {
  createTestHarness,
  RUN_ID,
  type TestHarness,
  THREAD_ID,
  USER_ID,
  WORKSPACE_ID,
} from "./helpers.js";

describe("Nine `/agent/*` Endpoints & Permission Enforcement (§1.2.1 ~ §1.2.7, Codex #22/#23/#24/#25)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("POST /agent/send delivers text & files, splits chunks, and handles duplicate/in_flight/partial", async () => {
    // 1. Standard send with tool_call_id and file attachment
    const sendRes = await h.app.agentRoutes.handleAgentSend(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      tool_call_id: "tc_001",
      turn_platform: "feishu",
      workspace_id: WORKSPACE_ID,
      target: null,
      text: "Here is your report.",
      files: [{ path: "reports/q3.pdf", workspace_id: WORKSPACE_ID }],
      reply: true,
      new_thread: false,
    });

    expect(sendRes.status).toBe(200);
    expect(sendRes.data.status).toBe("sent");
    expect(sendRes.data.current).toBe(true);
    expect(sendRes.data.duplicate).toBe(false);
    expect(sendRes.data.files).toEqual([
      { path: "reports/q3.pdf", status: "sent", reason: null },
    ]);
    expect(h.backend.deliveryClaims[0]?.delivery_key).toBe(`${RUN_ID}:tc_001`);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentFiles).toHaveLength(1);

    // 2. Fallback delivery_key when tool_call_id is absent (`${run_id}:notcid:${sha256}`)
    const keyNoTcid = DeliveryService.makeAgentSendDeliveryKey({
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      tool_call_id: null,
      text: "Same payload",
      target: "feishu:@me",
    });
    expect(keyNoTcid.startsWith(`${RUN_ID}:notcid:`)).toBe(true);

    // 3. Duplicate (`already_completed`)
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
      claim_status: "already_completed",
      status: "sent",
      platform: "feishu",
      target_address: "feishu:@me",
      is_current: true,
      first_response_json: {
        status: "sent",
        code: null,
        address: "feishu:@me",
        current: true,
        message: "Delivered to feishu:@me.",
        files: [],
      },
    });
    const dupRes = await h.app.agentRoutes.handleAgentSend(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      tool_call_id: "tc_001",
      text: "Here is your report.",
    });
    expect(dupRes.data.duplicate).toBe(true);
    expect(dupRes.data.status).toBe("sent");

    // 4. In-flight (`in_flight_or_unknown`)
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
      claim_status: "in_flight_or_unknown",
      status: "unknown",
      code: "in_flight",
      platform: "feishu",
      target_address: "feishu:@me",
      is_current: true,
    });
    const inFlightRes = await h.app.agentRoutes.handleAgentSend(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      tool_call_id: "tc_002",
      text: "Still sending",
    });
    expect(inFlightRes.data.status).toBe("unknown");
    expect(inFlightRes.data.code).toBe("in_flight");

    // 5. Multi-chunk partial failure (chunk 0 succeeds, chunk 1 fails -> status: "partial")
    h.feishuAdapter.setSendTextBehavior((opts) => {
      if (opts.chunkIndex === 1) {
        throw new ChannelDeliveryError("Rate limit exceeded", {
          outcome: "rate_limited",
          code: "rate_limited",
        });
      }
      return { messageId: "msg_chunk_0" };
    });
    const longText = "A".repeat(1500) + "\n" + "B".repeat(1500);
    const partialRes = await h.app.agentRoutes.handleAgentSend(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      tool_call_id: "tc_003",
      turn_platform: "feishu",
      text: longText,
    });
    expect(partialRes.data.status).toBe("partial");
    expect(partialRes.data.code).toBe("rate_limited");
  });

  it("GET /agent/targets & GET /agent/settings/available enforce `agent_messages`, workspace permissions, and `external` tier group restrictions (Codex #23)", async () => {
    h.discordAdapter.connected = false;
    const res = await h.app.agentRoutes.handleGetTargets(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      turn_platform: "feishu",
    });
    expect(res.status).toBe(200);
    expect(res.data.current).toEqual({ address: "feishu:@me" });
    expect(res.data.targets.map((t) => t.address)).toEqual([
      "feishu:@me",
      "feishu:oc_group1",
      "weixin:@me",
    ]);
    expect(res.data.unavailable).toEqual([
      {
        platform: "discord",
        reason: "unavailable",
        message: "The discord service is currently unavailable.",
      },
    ]);

    // When `user_tier === "external"`, group chats are excluded from both `/agent/targets` and `/agent/settings/available`
    const baseState = await h.backend.getChannelState({ user_id: USER_ID });
    vi.mocked(h.backend.getChannelState).mockResolvedValue({
      ...baseState,
      user_tier: "external",
    });
    h.feishuAdapter.setAvailableChats([
      { address: "feishu:oc_live2", name: "Live Group 2", kind: "group" },
    ]);

    const extTargets = await h.app.agentRoutes.handleGetTargets(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      turn_platform: "feishu",
    });
    expect(extTargets.data.targets.map((t) => t.address)).toEqual([
      "feishu:@me",
      "weixin:@me",
    ]);

    const extAvail = await h.app.agentRoutes.handleGetSettingsAvailable(USER_ID);
    expect(extAvail.data.apps.feishu?.chats.map((c) => c.address)).toEqual([
      "feishu:@me",
    ]);

    // When a platform is unlinked (`!identity`), `GET /agent/targets` reports `reason: "not_linked"` in `unavailable` (Grok #11)
    vi.mocked(h.backend.getChannelState).mockResolvedValueOnce({
      ...baseState,
      identities: {
        feishu: baseState.identities.feishu!,
      },
    });
    const unlinkedRes = await h.app.agentRoutes.handleGetTargets(USER_ID, {});
    expect(
      unlinkedRes.data.unavailable.some(
        (u) => u.platform === "weixin" && u.reason === "not_linked",
      ),
    ).toBe(true);
  });

  it("GET /agent/targets returns no targets for a platform when agent_messages.allowed is an empty array [] (channel_gateway.py:2598, 2610, Codex r5 #7)", async () => {
    const baseState = await h.backend.getChannelState({ user_id: USER_ID });
    vi.mocked(h.backend.getChannelState).mockResolvedValueOnce({
      ...baseState,
      settings: {
        ...baseState.settings,
        apps: {
          ...baseState.settings.apps,
          feishu: {
            ...baseState.settings.apps.feishu!,
            agent_messages: {
              enabled: true,
              allowed: [],
            },
          },
        },
      },
    });
    const emptyAllowedRes = await h.app.agentRoutes.handleGetTargets(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      turn_platform: "feishu",
    });
    expect(
      emptyAllowedRes.data.targets.some((t) => t.address.startsWith("feishu:")),
    ).toBe(false);
  });

  it("POST /agent/check-target rejects unregistered/unauthorized group chats and non-writable workspaces; GET /agent/automation-targets returns `default` + `error` (Codex #22/#24)", async () => {
    // 1. Bare platform name resolves to `@me`
    const bareCheck = await h.app.agentRoutes.handleCheckTarget(USER_ID, {
      address: "feishu",
      purpose: "automation",
    });
    expect(bareCheck.data).toEqual({
      ok: true,
      address: "feishu:@me",
      message: null,
    });

    // 2. Registered group chat in `chats` / `chat_bindings` with writable workspace succeeds
    const validGroupCheck = await h.app.agentRoutes.handleCheckTarget(USER_ID, {
      address: "feishu:oc_group1",
      purpose: "automation",
    });
    expect(validGroupCheck.data.ok).toBe(true);

    // 3. Unknown/unregistered group chat is rejected (Codex #24)
    const unknownGroupCheck = await h.app.agentRoutes.handleCheckTarget(
      USER_ID,
      {
        address: "feishu:oc_unknown_stranger_group",
        purpose: "automation",
      },
    );
    expect(unknownGroupCheck.data.ok).toBe(false);
    expect(unknownGroupCheck.data.message).toContain("permission");

    // 4. Group chat bound to a non-writable workspace is rejected via I-3 `workspace_list` (Codex r2 #20, Grok r2 #1)
    vi.mocked(h.backend.sessionCommand).mockResolvedValueOnce({
      ok: true,
      current_workspace_id: null,
      home_workspace_id: null,
      workspaces: [],
    });
    const nonWritableGroupCheck = await h.app.agentRoutes.handleCheckTarget(
      USER_ID,
      {
        address: "feishu:oc_group1",
        purpose: "automation",
      },
    );
    expect(nonWritableGroupCheck.data.ok).toBe(false);
    expect(nonWritableGroupCheck.data.message).toContain("write access");

    // 4b. Group chat present in global `chat_bindings` (another user's group) but NOT in user's `appCfg.chats` is rejected (Codex r2 #20)
    const baseState = await h.backend.getChannelState({ user_id: USER_ID });
    vi.mocked(h.backend.getChannelState).mockResolvedValueOnce({
      ...baseState,
      chat_bindings: [
        ...baseState.chat_bindings,
        {
          platform: "feishu",
          chat_address: "feishu:oc_other_user_group",
          chat_name: "Other User Group",
          workspace_id: WORKSPACE_ID,
          bound_by_user_id: "user_bob_999",
          last_used_at: 1700000000,
        },
      ],
    });
    const otherUserGroupCheck = await h.app.agentRoutes.handleCheckTarget(
      USER_ID,
      {
        address: "feishu:oc_other_user_group",
        purpose: "automation",
      },
    );
    expect(otherUserGroupCheck.data.ok).toBe(false);

    // 5. GET /agent/automation-targets includes `default` and `error: null` (matching `DeliveryPicker.tsx`, Codex #22)
    const autoTargets = await h.app.agentRoutes.handleGetAutomationTargets(
      USER_ID,
      WORKSPACE_ID,
    );
    expect(autoTargets.data.apps.feishu?.default_address).toBe(
      "feishu:oc_group1",
    );
    expect(autoTargets.data.apps.feishu?.default).toEqual({
      address: "feishu:oc_group1",
      name: "Alpha Research Group",
      via: "workspace",
    });
    expect(autoTargets.data.apps.feishu?.resolves_to).toEqual({
      address: "feishu:oc_group1",
      name: "Alpha Research Group",
      via: "workspace",
    });
    expect(autoTargets.data.apps.feishu?.error).toBeNull();

    // When `workspace_id` is not bound and `preferred === "feishu:@me"`, `via` is `"dm"` (Grok #8)
    const autoTargetsDm = await h.app.agentRoutes.handleGetAutomationTargets(
      USER_ID,
      null,
    );
    expect(autoTargetsDm.data.apps.feishu?.default?.via).toBe("dm");
  });

  it("GET & PUT /agent/settings and POST /agent/automation-runs / /finish (with 20,000 char limit & `via: fallback`, Codex #25)", async () => {
    expect(MAX_AUTOMATION_FINAL_TEXT_CHARS).toBe(20_000);

    const getRes = await h.app.agentRoutes.handleGetSettings(USER_ID);
    expect(getRes.status).toBe(200);
    expect(getRes.data.version).toBe("v1");

    // 409 conflict
    vi.mocked(h.backend.putChannelSettings).mockResolvedValueOnce({
      status: 409,
      data: { code: "version_conflict", message: "Version mismatch" },
    });
    const put409 = await h.app.agentRoutes.handlePutSettings(USER_ID, {
      version: "stale",
      settings: getRes.data.settings,
    });
    expect(put409.status).toBe(409);

    // 403 group permission denied -> mapped to 400 with problems
    vi.mocked(h.backend.putChannelSettings).mockResolvedValueOnce({
      status: 403,
      data: {
        detail:
          "You do not have permission to manage group chat feishu:oc_secret",
      },
    });
    const put403 = await h.app.agentRoutes.handlePutSettings(USER_ID, {
      version: "v1",
      settings: getRes.data.settings,
    });
    expect(put403.status).toBe(400);
    expect(put403.data.problems?.[0]?.message).toContain("feishu:oc_secret");

    // Shape A: completed with final_text -> sends fallback summary (`via: "fallback"`)
    const execId = "22222222-3333-4444-5555-666666666666";
    const finishA = await h.app.agentRoutes.handleFinishAutomationRun(
      USER_ID,
      execId,
      {
        status: "completed",
        final_text: "Summary of market moves.",
      },
    );
    expect(finishA.status).toBe(200);
    expect(finishA.data.targets).toEqual([
      {
        entry: "feishu",
        address: "feishu:@me",
        name: "Alice (feishu DM)",
        reached: true,
        via: "fallback",
        error: null,
      },
    ]);

    // Shape A: failed run -> sends status notice with `reached: true, via: "notice"` (Grok #7)
    const execFailId = "22222222-3333-4444-5555-777777777777";
    const finishFail = await h.app.agentRoutes.handleFinishAutomationRun(
      USER_ID,
      execFailId,
      {
        status: "failed",
      },
    );
    expect(finishFail.status).toBe(200);
    expect(finishFail.data.targets).toEqual([
      {
        entry: "feishu",
        address: "feishu:@me",
        name: "Alice (feishu DM)",
        reached: true,
        via: "notice",
        error: null,
      },
    ]);
  });
});
