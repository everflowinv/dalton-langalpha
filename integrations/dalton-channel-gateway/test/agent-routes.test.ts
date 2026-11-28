import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChannelDeliveryError } from "../src/channels/adapter.js";
import { notLinkedTargetMessage, personalChannelBindingsUrl } from "../src/commands.js";
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
    expect(res.data.settings_url).toBe(personalChannelBindingsUrl("https://app.ginlix.ai"));
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
    expect(unlinkedRes.data.settings_url).toBe(personalChannelBindingsUrl("https://app.ginlix.ai"));
    const unlinked = unlinkedRes.data.unavailable.filter((u) => u.reason === "not_linked");
    expect(unlinked.length).toBeGreaterThan(0);
    for (const row of unlinked) {
      expect(row.message).toBe(notLinkedTargetMessage("https://app.ginlix.ai", row.platform));
      expect(row.message).not.toContain("/settings/channels");
      expect(row.message).not.toContain("6-CHAR");
    }
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

  it("does not show another Discord guild to an internal user, while an admin still sees the bot directory", async () => {
    const baseState = await h.backend.getChannelState({ user_id: USER_ID });
    const visible = "discord:700000011/700000013";
    const hidden = "discord:700000012/700000014";
    const internalState = {
      ...baseState,
      user_tier: "internal",
      chat_bindings: [
        ...baseState.chat_bindings,
        {
          platform: "discord",
          chat_address: visible,
          chat_name: "alpha-room",
          workspace_id: WORKSPACE_ID,
          bound_by_user_id: USER_ID,
          last_used_at: null,
        },
      ],
    };
    vi.mocked(h.backend.getChannelState).mockResolvedValue(internalState);
    h.discordAdapter.connected = true;
    h.discordAdapter.setAvailableChats([
      { address: visible, name: "#alpha-room", kind: "channel" },
      { address: hidden, name: "#beta-secret-room", kind: "channel" },
    ]);

    const internalAvail = await h.app.agentRoutes.handleGetSettingsAvailable(USER_ID);
    const internalChats = internalAvail.data.apps.discord?.chats ?? [];
    expect(internalChats.map((chat) => chat.address)).toContain("discord:@me");
    expect(internalChats.map((chat) => chat.address)).toContain(visible);
    expect(internalChats.map((chat) => chat.address)).not.toContain(hidden);
    expect(JSON.stringify(internalChats)).not.toContain("beta-secret");

    vi.mocked(h.backend.getChannelState).mockResolvedValue({
      ...internalState,
      user_tier: "admin",
    });
    const adminAvail = await h.app.agentRoutes.handleGetSettingsAvailable(USER_ID);
    const adminAddresses = (adminAvail.data.apps.discord?.chats ?? []).map((chat) => chat.address);
    expect(adminAddresses).toContain(visible);
    expect(adminAddresses).toContain(hidden);
    expect(adminAddresses).toContain("discord:@me");
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

  it("unifies automation finish delivery dedup key across <uuid>.2 alias and startup recovery (Codex r2 #5)", async () => {
    const canonicalExecId = "22222222-3333-4444-5555-888888888888";
    const retryAliasId = `${canonicalExecId}.2`;

    // Backend normalizes `<uuid>.2` to the canonical DB UUID in I-8 finishAutomationRun response
    vi.mocked(h.backend.finishAutomationRun).mockImplementationOnce(
      async () => ({
        status: 200,
        data: {
          execution_id: canonicalExecId,
          status: "finishing",
          previous_status: "registered",
          user_id: USER_ID,
          workspace_id: WORKSPACE_ID,
          thread_id: THREAD_ID,
          run_id: null,
          automation_name: "Delta Report",
          finish_status: "completed",
          final_text: "Summary after recipient edit.",
          targets: [
            {
              entry: "feishu:oc_new_group",
              address: "feishu:oc_new_group",
              name: "New Group",
              ok: true,
              message: null,
              agent_sent: false,
            },
          ],
        },
      }),
    );
    // Simulate gateway crash right before mark_finished=true write completes
    vi.mocked(h.backend.finishAutomationRun).mockResolvedValueOnce({
      status: 500,
      data: null,
    });

    const seenKeys = new Set<string>();
    const origClaim = vi.mocked(h.backend.authorizeAndClaimDelivery).getMockImplementation();
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(async (req) => {
      if (seenKeys.has(req.delivery_key)) {
        return {
          claim_status: "already_completed",
          status: "sent",
          platform: "feishu",
          target_address: req.target_address,
          is_current: true,
          first_response_json: {
            status: "sent",
            code: null,
            address: req.target_address,
            current: true,
            message: `Delivered to ${req.target_address}.`,
            files: [],
          },
        };
      }
      seenKeys.add(req.delivery_key);
      return origClaim!(req);
    });

    // 1. Normal `/finish` called with `<uuid>.2` alias -> sends using canonical UUID key
    await h.app.agentRoutes.handleFinishAutomationRun(USER_ID, retryAliasId, {
      status: "completed",
      final_text: "Summary after recipient edit.",
    });

    // 2. Startup recovery runs with canonical DB UUID (`dalton_channel_automation_runs.execution_id`)
    vi.mocked(h.backend.finishAutomationRun).mockResolvedValueOnce({
      status: 200,
      data: {
        execution_id: canonicalExecId,
        status: "finished",
        previous_status: "finishing",
        user_id: USER_ID,
        workspace_id: WORKSPACE_ID,
        thread_id: THREAD_ID,
        run_id: null,
        automation_name: "Delta Report",
        finish_status: "completed",
        final_text: "Summary after recipient edit.",
        targets: [],
      },
    });
    await h.app.agentRoutes.executeAutomationRunFinish({
      executionId: canonicalExecId,
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: null,
      automationName: "Delta Report",
      finishStatus: "completed",
      finalText: "Summary after recipient edit.",
      targets: [
        {
          entry: "feishu:oc_new_group",
          address: "feishu:oc_new_group",
          name: "New Group",
          ok: true,
          message: null,
          agent_sent: false,
        },
      ],
    });

    const expectedKey = `automation:${canonicalExecId}:feishu:oc_new_group:summary`;
    const deliveryKeys = h.backend.deliveryClaims.map((c) => c.delivery_key);
    expect(deliveryKeys).toEqual([expectedKey, expectedKey]);
    expect(deliveryKeys.every((k) => !k.includes(".2"))).toBe(true);
    // Platform adapter sent strictly once despite crash before mark_finished
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
  });

  it("rewrites sandbox links before chunking across Feishu, Discord, and Weixin deliveries and falls back on localhost WEB_BASE_URL", async () => {
    const sampleText = [
      "详见 [季报](/home/workspace/results/Q3 报告.md) 与 [外部](https://example.com/a)。",
      `跨区：[附图](__wsref__/${WORKSPACE_ID}/charts/fig.png)，预览：[看板](http://localhost:8000/app)。`,
      "裸路径：/home/daytona/data/raw.csv，代码：`/home/workspace/keep.py`。",
    ].join("\n");

    const expectedPublic = [
      `详见 [季报](https://app.ginlix.ai/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2FQ3%20%E6%8A%A5%E5%91%8A.md) 与 [外部](https://example.com/a)。`,
      `跨区：[附图](https://app.ginlix.ai/chat/${WORKSPACE_ID}?file=charts%2Ffig.png)，预览：看板。`,
      "裸路径：data/raw.csv，代码：`/home/workspace/keep.py`。",
    ].join("\n");

    // 1. Feishu via POST /agent/send
    const feishuSend = await h.app.agentRoutes.handleAgentSend(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      tool_call_id: "tc_links_feishu",
      turn_platform: "feishu",
      target: "feishu:@me",
      text: sampleText,
    });
    expect(feishuSend.status).toBe(200);
    expect(feishuSend.data.status).toBe("sent");
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(expectedPublic);

    // 2. Discord via DeliveryService fallback path
    const discordFallback = await h.app.deliveryService.executeDelivery({
      deliveryKey: `fallback:${RUN_ID}:discord-links`,
      kind: "fallback",
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      platform: "discord",
      target: "discord:@me",
      text: sampleText,
    });
    expect(discordFallback.status).toBe("sent");
    expect(h.discordAdapter.sentTexts.at(-1)?.text).toBe(expectedPublic);

    // 3. Weixin via DeliveryService fallback path
    const weixinFallback = await h.app.deliveryService.executeDelivery({
      deliveryKey: `fallback:${RUN_ID}:weixin-links`,
      kind: "fallback",
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      platform: "weixin",
      target: "weixin:@me",
      text: sampleText,
    });
    expect(weixinFallback.status).toBe("sent");
    expect(h.weixinAdapter.sentTexts.at(-1)?.text).toBe(expectedPublic);

    // 4. Localhost WEB_BASE_URL degrades links to plain-text workspace hints
    const localHarness = createTestHarness({
      env: { WEB_BASE_URL: "http://localhost:5173" },
    });
    try {
      const localRes = await localHarness.app.agentRoutes.handleAgentSend(USER_ID, {
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        tool_call_id: "tc_links_local",
        turn_platform: "feishu",
        target: "feishu:@me",
        text: "查看 [报告](/home/workspace/results/report.md) 与 [预览](http://127.0.0.1:8000/)。",
      });
      expect(localRes.data.status).toBe("sent");
      expect(localHarness.feishuAdapter.sentTexts.at(-1)?.text).toBe(
        "查看 报告（在网页工作区查看：results/report.md） 与 预览。",
      );
    } finally {
      await localHarness.cleanup();
    }
  });
});
