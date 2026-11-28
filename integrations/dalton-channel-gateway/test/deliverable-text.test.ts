import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { DeliveryService } from "../src/delivery-service.js";
import {
  CHANNEL_EMPTY_AFTER_TOOLS_NOTICE,
  CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE,
  DeliverableTextAccumulator,
  buildImageCorrectionText,
  computeSendPayloadDigest,
  containsUnresolvedSandboxImagePath,
  extractDeliverableTextFromEvents,
  resolveChannelFallbackText,
  resolveDeliveryImageUrls,
} from "../src/shared/deliverable-text.js";
import {
  createTestHarness,
  makeSseEventsFromFixture,
  RUN_ID,
  THREAD_ID,
  USER_ID,
} from "./helpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const vectorsPath = resolve(
  __dirname,
  "../../../tests/fixtures/dalton/deliverable_text_vectors.json",
);
const { vectors } = JSON.parse(readFileSync(vectorsPath, "utf-8")) as {
  vectors: Array<{
    name: string;
    events: Array<{ event: string; data: Record<string, unknown> }>;
    expected_text: string;
    expected_saw_business_tool_call: boolean;
  }>;
};

describe("shared deliverable-text vectors (§5.5)", () => {
  for (const vec of vectors) {
    it(`matches vector: ${vec.name}`, () => {
      const res = extractDeliverableTextFromEvents(vec.events);
      expect(res.text).toBe(vec.expected_text);
      expect(res.sawBusinessToolCall).toBe(vec.expected_saw_business_tool_call);
    });
  }
});

describe("DeliverableTextAccumulator live streaming & continuation (§5.5, §6.5)", () => {
  it("concatenates live output_cap continuation from the very first msg_1 chunk", () => {
    const acc = new DeliverableTextAccumulator();
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "msg_0",
      content_type: "text",
      content: "前半段",
    });
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "msg_1",
      continued_from_message_id: "msg_0",
      content_type: "text",
      content: "后",
    });
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "msg_1",
      continued_from_message_id: "msg_0",
      content_type: "text",
      content: "半段",
    });
    expect(acc.result()).toEqual({
      text: "前半段后半段",
      sawBusinessToolCall: false,
    });
  });

  it("retracts discarded fragment and keeps subsequent exhausted notice", () => {
    const acc = new DeliverableTextAccumulator();
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "m-sym",
      content_type: "text",
      content: "═",
    });
    acc.pushEvent("message_discarded", {
      agent: "main",
      id: "m-sym",
    });
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "dalton-notice-1",
      content_type: "text",
      content:
        "这一轮模型只输出了异常的极短片段，未能生成完整回复。如果本轮包含早期操作，请先核对结果，再决定是否重新发送。",
      finish_reason: "stop",
    });
    expect(acc.result().text).toBe(
      "这一轮模型只输出了异常的极短片段，未能生成完整回复。如果本轮包含早期操作，请先核对结果，再决定是否重新发送。",
    );
  });
});

describe("resolveChannelFallbackText four send_message branches (§5.5)", () => {
  it("1. send_message to another chat (not current) still delivers empty-after-tools notice", () => {
    expect(
      resolveChannelFallbackText({
        extractedText: "",
        sawBusinessToolCall: true,
        sentToCurrentCount: 0,
        hasDeliveredCurrentAgentSend: false,
        hasFailedCurrentAgentSend: false,
      }),
    ).toBe(CHANNEL_EMPTY_AFTER_TOOLS_NOTICE);
  });

  it("2. send_message to current chat failed delivers send-failed fallback notice", () => {
    expect(
      resolveChannelFallbackText({
        extractedText: "",
        sawBusinessToolCall: true,
        sentToCurrentCount: 0,
        hasDeliveredCurrentAgentSend: false,
        hasFailedCurrentAgentSend: true,
      }),
    ).toBe(CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE);
  });

  it("3. send_message to current chat delivered files-only skips redundant text notice", () => {
    expect(
      resolveChannelFallbackText({
        extractedText: "",
        sawBusinessToolCall: true,
        sentToCurrentCount: 0,
        hasDeliveredCurrentAgentSend: true,
        hasFailedCurrentAgentSend: false,
      }),
    ).toBe("");
  });

  it("4. send_message to current chat delivered text skips fallback notice", () => {
    expect(
      resolveChannelFallbackText({
        extractedText: "",
        sawBusinessToolCall: true,
        sentToCurrentCount: 1,
        hasDeliveredCurrentAgentSend: true,
        hasFailedCurrentAgentSend: false,
      }),
    ).toBe("");
  });

  it("5. DALTON_STRICT_DELIVERABLE_TEXT=0 from backend falls back to legacy pre-tool text", () => {
    const acc = new DeliverableTextAccumulator();
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "m-pre",
      content_type: "text",
      content: "工具前的过渡话术",
    });
    acc.pushEvent("tool_calls", {
      agent: "main",
      tool_calls: [{ id: "tc-1", name: "Bash", args: {} }],
    });
    acc.pushEvent("tool_call_result", {
      agent: "main",
      tool_call_id: "tc-1",
      content: "ok",
    });
    // Strict mode: extracted text is empty -> deterministic notice.
    expect(
      resolveChannelFallbackText({
        extractedText: acc.result().text,
        sawBusinessToolCall: acc.result().sawBusinessToolCall,
        sentToCurrentCount: 0,
        strictDeliverableTextEnabled: true,
        legacyText: acc.legacyResult(),
      }),
    ).toBe(CHANNEL_EMPTY_AFTER_TOOLS_NOTICE);
    // Rollback mode (backend strict_deliverable_text_enabled=false): legacy pre-tool text.
    expect(
      resolveChannelFallbackText({
        extractedText: acc.result().text,
        sawBusinessToolCall: acc.result().sawBusinessToolCall,
        sentToCurrentCount: 0,
        strictDeliverableTextEnabled: false,
        legacyText: acc.legacyResult(),
      }),
    ).toBe("工具前的过渡话术");
  });
});

describe("R2 fixes: chained exhausted notice, image parity, imgfix, mismatched discard, retry send dedup", () => {
  it("appends chained output_cap exhausted notice onto prefix segments", () => {
    const acc = new DeliverableTextAccumulator();
    acc.pushEvent("message_chunk", { agent: "main", id: "m-1", content_type: "text", content: "前半段" });
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "m-2",
      continued_from_message_id: "m-1",
      content_type: "text",
      content: "后半段",
    });
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "dalton-notice-1",
      continued_from_message_id: "m-2",
      content_type: "text",
      content: "模型回复多次超出输出上限而被截断",
      finish_reason: "stop",
    });
    expect(acc.result().text).toBe("前半段后半段模型回复多次超出输出上限而被截断");
  });

  it("retracts streamed fragment when message_discarded carries mismatched stream_id / ids", () => {
    const acc = new DeliverableTextAccumulator();
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "lc_run--chunk-1",
      content_type: "text",
      content: "═",
    });
    acc.pushEvent("message_discarded", {
      agent: "main",
      id: "msg-final-1",
      stream_id: "lc_run--chunk-1",
      ids: ["msg-final-1", "lc_run--chunk-1"],
    });
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "msg-final-2",
      content_type: "text",
      content: "正常结论",
    });
    expect(acc.result().text).toBe("正常结论");
  });

  it("resolves sandbox image paths from run_end.images identically to replay", () => {
    const acc = new DeliverableTextAccumulator();
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "m-img",
      content_type: "text",
      content: "图表如下：\n![图](charts/drop_cn.png)",
    });
    acc.pushEvent("run_end", {
      run_id: RUN_ID,
      outcome: "completed",
      images: { "charts/drop_cn.png": "https://cdn.example/drop_cn.png" },
    });
    expect(acc.result().text).toBe("图表如下：\n![图](https://cdn.example/drop_cn.png)");
    expect(containsUnresolvedSandboxImagePath(acc.result().text)).toBe(false);
    expect(containsUnresolvedSandboxImagePath("![图](charts/drop_cn.png)")).toBe(true);
  });

  it("allows corrected reply with resolved image URL even when prior fallback already delivered raw sandbox path", async () => {
    const h = createTestHarness();
    try {
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
        claim_status: "already_completed",
        status: "sent",
        platform: "feishu",
        target_address: "feishu:@me",
        is_current: true,
        first_response_json: {
          status: "sent",
          unresolved_sandbox_images: true,
        },
      });
      const orch = h.app.orchestrator as unknown as {
        settleTurnFallbackAndPartialNotice: (p: Record<string, unknown>) => Promise<boolean>;
      };
      const ok = await orch.settleTurnFallbackAndPartialNotice({
        fallbackDeliveryKey: `fallback:${RUN_ID}`,
        partialNoticeKeyId: RUN_ID,
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        platform: "feishu",
        targetAddress: "feishu:@me",
        trimmedText: "图表如下：\n![图](https://cdn.example/drop_cn.png)",
        sentToCurrentCount: 1,
      });
      expect(ok).toBe(true);
      expect(
        h.backend.deliveryClaims.some(
          (c) => c.delivery_key === `fallback:${RUN_ID}:imgfix`,
        ),
      ).toBe(true);
      expect(h.feishuAdapter.sentTexts).toHaveLength(1);
      expect(h.feishuAdapter.sentTexts[0]?.text).toContain("https://cdn.example/drop_cn.png");
    } finally {
      await h.cleanup();
    }
  });

  it("deduplicates retry-generated send_message via idempotency_key and retry_dedup_by_target", async () => {
    const key = DeliveryService.makeAgentSendDeliveryKey({
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      tool_call_id: "tc-retry-new",
      idempotency_key: `${RUN_ID}:tc-orig`,
      target: "feishu:@me",
      text: "重复直发内容",
    });
    expect(key).toBe(`${RUN_ID}:tc-orig`);

    const h = createTestHarness();
    try {
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
      const res = await h.app.agentRoutes.handleAgentSend(USER_ID, {
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        tool_call_id: "tc-retry-new",
        idempotency_key: `${RUN_ID}:tc-orig`,
        retry_dedup_by_target: true,
        target: "feishu:@me",
        text: "重复直发内容",
      });
      expect(res.data.duplicate).toBe(true);
      expect(res.data.status).toBe("sent");
      expect(h.feishuAdapter.sentTexts).toHaveLength(0);
      expect(h.backend.deliveryClaims[0]?.delivery_key).toBe(`${RUN_ID}:tc-orig`);
      expect(h.backend.deliveryClaims[0]?.retry_dedup_by_target).toBe(true);
    } finally {
      await h.cleanup();
    }
  });
});

describe("R3 fixes: per-message image parity, agent_send imgfix, replay attach & automation recovery rollback", () => {
  it("resolves per-message captured images in live stream when same path is rewritten mid-turn", () => {
    const acc = new DeliverableTextAccumulator();
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "m-1",
      content_type: "text",
      content: "第一版：![图](charts/a.png)\n",
    });
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "m-2",
      continued_from_message_id: "m-1",
      content_type: "text",
      content: "第二版：![图](charts/a.png)",
    });
    acc.pushEvent("run_end", {
      run_id: RUN_ID,
      outcome: "completed",
      images: { "charts/a.png": "https://cdn.example/v2.png" },
      images_by_message: {
        "m-1": { "charts/a.png": "https://cdn.example/v1.png" },
        "m-2": { "charts/a.png": "https://cdn.example/v2.png" },
      },
    });
    expect(acc.result().text).toBe(
      "第一版：![图](https://cdn.example/v1.png)\n第二版：![图](https://cdn.example/v2.png)",
    );
  });

  it("records unresolved_sandbox_images on delivered agent_send and allows :imgfix correction", async () => {
    const h = createTestHarness();
    try {
      const sendRes = await h.app.agentRoutes.handleAgentSend(USER_ID, {
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        tool_call_id: "tc-img-raw",
        turn_platform: "feishu",
        target: "feishu:@me",
        text: "图表如下：\n![图](charts/raw_only.png)",
      });
      expect(sendRes.data.status).toBe("sent");
      const progressCalls = vi.mocked(h.backend.updateDeliveryProgress).mock.calls;
      const progressRec = progressCalls
        .map((c) => c[0])
        .find((p) => p.delivery_key === `${RUN_ID}:tc-img-raw` && p.status === "sent");
      const firstResp = progressRec?.first_response_json as
        | Record<string, unknown>
        | undefined;
      expect(firstResp?.unresolved_sandbox_images).toBe(true);
      expect(firstResp?.unresolved_sandbox_image_paths).toEqual([
        "charts/raw_only.png",
      ]);
      expect(firstResp?.delivered_text).toBe(
        "图表如下：\n![图](charts/raw_only.png)",
      );
      expect(typeof firstResp?.payload_digest).toBe("string");

      // Now run settles with resolved image URL: fallback claim returns "claimed" (agent_send already sent),
      // and :imgfix is generated strictly from the original delivered_text!
      vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
        claim_status: "claimed",
        platform: "feishu",
        target_address: "feishu:@me",
        is_current: true,
      });
      const orch = h.app.orchestrator as unknown as {
        settleTurnFallbackAndPartialNotice: (p: Record<string, unknown>) => Promise<boolean>;
      };
      const ok = await orch.settleTurnFallbackAndPartialNotice({
        fallbackDeliveryKey: `fallback:${RUN_ID}`,
        partialNoticeKeyId: RUN_ID,
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        platform: "feishu",
        targetAddress: "feishu:@me",
        trimmedText: "已完成",
        sentToCurrentCount: 1,
        hasUnresolvedCurrentAgentSendImages: true,
        unresolvedCurrentAgentSends: [
          {
            delivery_key: `${RUN_ID}:tc-img-raw`,
            delivered_text: "图表如下：\n![图](charts/raw_only.png)",
            unresolved_image_paths: ["charts/raw_only.png"],
          },
        ],
        resolvedImageUrls: {
          "charts/raw_only.png": "https://cdn.example/resolved.png",
        },
      });
      expect(ok).toBe(true);
      expect(
        h.backend.deliveryClaims.some(
          (c) => c.delivery_key === `fallback:${RUN_ID}:imgfix`,
        ),
      ).toBe(true);
      const imgfixSent = h.feishuAdapter.sentTexts[h.feishuAdapter.sentTexts.length - 1];
      expect(imgfixSent?.text).toBe("图表如下：\n![图](https://cdn.example/resolved.png)");
      expect(imgfixSent?.text).not.toBe("已完成");
    } finally {
      await h.cleanup();
    }
  });

  it("consumeInFlightReplayRunStream returns distinct strict and legacy text for rollback flag", async () => {
    const h = createTestHarness();
    try {
      vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
        isSse: true,
        events: (async function* () {
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              id: "m-pre",
              content_type: "text",
              content: "工具前旧文案",
            }),
          };
          yield {
            event: "tool_calls",
            data: JSON.stringify({
              agent: "main",
              tool_calls: [{ id: "tc-1", name: "Bash", args: {} }],
            }),
          };
          yield {
            event: "tool_call_result",
            data: JSON.stringify({
              agent: "main",
              tool_call_id: "tc-1",
              content: "ok",
            }),
          };
          yield {
            event: "run_end",
            data: JSON.stringify({ run_id: RUN_ID, outcome: "completed" }),
          };
        })(),
      } as never);

      const orch = h.app.orchestrator as unknown as {
        consumeInFlightReplayRunStream: (p: Record<string, unknown>) => Promise<{
          settled: boolean;
          assistantText: string;
          legacyText: string;
          sawBusinessToolCall: boolean;
        }>;
      };
      const res = await orch.consumeInFlightReplayRunStream({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        lastEventId: null,
        preCursorText: "",
        preCursorLegacyText: "",
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
      });
      expect(res.settled).toBe(true);
      expect(res.assistantText).toBe("");
      expect(res.legacyText).toBe("工具前旧文案");
      expect(res.sawBusinessToolCall).toBe(true);
    } finally {
      await h.cleanup();
    }
  });

  it("recoverSingleAutomationRun forwards strict_deliverable_text_enabled=false so empty summary is suppressed", async () => {
    const h = createTestHarness();
    try {
      const execSpy = vi.spyOn(h.app.agentRoutes, "executeAutomationRunFinish").mockResolvedValueOnce([]);
      const orch = h.app.orchestrator as unknown as {
        recoverSingleAutomationRun: (
          row: Record<string, unknown>,
          recoveryStrict?: boolean,
        ) => Promise<void>;
      };
      await orch.recoverSingleAutomationRun(
        {
          execution_id: "exec-r3-1",
          user_id: USER_ID,
          workspace_id: null,
          thread_id: THREAD_ID,
          run_id: RUN_ID,
          automation_name: "早报",
          entries: ["feishu:@me"],
          status: "finishing",
          finish_status: "completed",
          final_text: null,
          targets: [],
          strict_deliverable_text_enabled: false,
        },
        true,
      );
      expect(execSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          executionId: "exec-r3-1",
          strictDeliverableTextEnabled: false,
        }),
      );
    } finally {
      await h.cleanup();
    }
  });
});

describe("R4 fixes: empty tool-call clear, workspace-aware dedup digest, original-bound :imgfix via live & replay", () => {
  it("preserves continuation prefix across empty tool_use finish_reason without valid tool_calls", () => {
    const acc = new DeliverableTextAccumulator();
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "lc_run--cap",
      content_type: "text",
      content: "前半段",
      finish_reason: "length",
    });
    // Empty tool_use finish frame (no valid tool_calls event) MUST NOT clear retained prefix
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "lc_run--empty-tc",
      content_type: "text",
      finish_reason: "tool_calls",
    });
    acc.pushEvent("tool_calls", {
      agent: "main",
      id: "lc_run--empty-tc",
      tool_calls: [],
    });
    acc.pushEvent("message_discarded", {
      agent: "main",
      id: "m-empty-tc",
      stream_id: "lc_run--empty-tc",
      ids: ["m-empty-tc", "lc_run--empty-tc"],
    });
    acc.pushEvent("message_chunk", {
      agent: "main",
      id: "lc_run--tail",
      continued_from_message_id: "lc_run--cap",
      content_type: "text",
      content: "后半段",
      finish_reason: "stop",
    });
    expect(acc.result()).toEqual({
      text: "前半段后半段",
      sawBusinessToolCall: false,
    });
  });

  it("computes workspace-aware attachment digest matching Python and distinguishes same filename across two workspaces", () => {
    const wsA = "11111111-1111-4111-8111-111111111111";
    const wsB = "22222222-2222-4222-8222-222222222222";
    const digestAFromStr = computeSendPayloadDigest("请查收报告", ["report.pdf"], wsA);
    const digestAFromRef = computeSendPayloadDigest(
      "请查收报告",
      [{ path: "report.pdf", workspace_id: wsA }],
      wsB,
    );
    const digestBFromRef = computeSendPayloadDigest(
      "请查收报告",
      [{ path: "report.pdf", workspace_id: wsB }],
      wsA,
    );
    const expectedCanonicalA = JSON.stringify({
      files: [{ path: "report.pdf", workspace_id: wsA }],
      text: "请查收报告",
    });
    const expectedHashA = createHash("sha256")
      .update(expectedCanonicalA, "utf8")
      .digest("hex");

    expect(digestAFromStr).toBe(expectedHashA);
    expect(digestAFromStr).toBe(digestAFromRef);
    expect(digestAFromRef).not.toBe(digestBFromRef);
  });

  it("buildImageCorrectionText rewrites only original delivered text and returns null when unresolved path is not resolved", () => {
    expect(
      buildImageCorrectionText({
        originalText: "图表如下：\n![图](charts/raw.png)",
        unresolvedImagePaths: ["charts/raw.png"],
        resolvedImageUrls: { "charts/raw.png": "https://cdn.example/raw.png" },
      }),
    ).toBe("图表如下：\n![图](https://cdn.example/raw.png)");
    expect(
      buildImageCorrectionText({
        originalText: "图表如下：\n![图](charts/raw.png)",
        unresolvedImagePaths: ["charts/raw.png"],
        resolvedImageUrls: { "charts/other.png": "https://cdn.example/other.png" },
      }),
    ).toBeNull();
  });

  it("live SSE entry delivers :imgfix from original agent_send even when fallback text is empty or short acknowledgement", async () => {
    // Case A: empty fallback text after send_message tool call -> still enters settle and sends resolved original
    const hEmpty = createTestHarness();
    try {
      const origText = "图表如下：\n![图](charts/live_raw.png)";
      vi.mocked(hEmpty.backend.updateInboundProgress).mockResolvedValue({
        ok: true,
        run_id: RUN_ID,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
        sent_to_current_count: 1,
        has_delivered_current_agent_send: true,
        has_failed_current_agent_send: false,
        has_unresolved_current_agent_send_images: true,
        unresolved_current_agent_sends: [
          {
            delivery_key: `${RUN_ID}:tc-send-1`,
            delivered_text: origText,
            unresolved_image_paths: ["charts/live_raw.png"],
          },
        ],
        strict_deliverable_text_enabled: true,
      });
      const liveEvents = [
        {
          event: "metadata",
          data: { thread_id: THREAD_ID, run_id: RUN_ID, turn_index: 1 },
        },
        {
          event: "tool_calls",
          data: {
            agent: "main",
            id: "m-tc",
            tool_calls: [{ id: "tc-send-1", name: "send_message", args: { text: origText } }],
          },
        },
        {
          event: "run_end",
          data: {
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            outcome: "completed",
            images: { "charts/live_raw.png": "https://cdn.example/live_resolved.png" },
          },
        },
      ];
      vi.mocked(hEmpty.backend.createThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(liveEvents),
      });
      vi.mocked(hEmpty.backend.appendThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(liveEvents),
      });

      await hEmpty.app.orchestrator.handleInboundEvent({
        platform: "feishu",
        messageId: "om_r4_imgfix_empty",
        platformUserId: "ou_alice",
        chatType: "dm",
        chatId: "oc_dm_alice",
        chatAddress: "feishu:@me",
        threadScopeKey: "feishu:dm:ou_alice",
        text: "把图发给我",
      });

      expect(
        hEmpty.backend.deliveryClaims.some(
          (c) => c.delivery_key === `fallback:${RUN_ID}:imgfix`,
        ),
      ).toBe(true);
      expect(hEmpty.feishuAdapter.sentTexts).toHaveLength(1);
      expect(hEmpty.feishuAdapter.sentTexts[0]?.text).toBe(
        "图表如下：\n![图](https://cdn.example/live_resolved.png)",
      );
    } finally {
      await hEmpty.cleanup();
    }

    // Case B: model outputs short "已完成" after send_message, while image URL is NOT yet available ->
    // MUST NOT send "已完成" as :imgfix!
    const hAck = createTestHarness();
    try {
      const origText = "图表如下：\n![图](charts/unavail.png)";
      vi.mocked(hAck.backend.updateInboundProgress).mockResolvedValue({
        ok: true,
        run_id: RUN_ID,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
        sent_to_current_count: 1,
        has_delivered_current_agent_send: true,
        has_failed_current_agent_send: false,
        has_unresolved_current_agent_send_images: true,
        unresolved_current_agent_sends: [
          {
            delivery_key: `${RUN_ID}:tc-send-2`,
            delivered_text: origText,
            unresolved_image_paths: ["charts/unavail.png"],
          },
        ],
        strict_deliverable_text_enabled: true,
      });
      const ackEvents = [
        {
          event: "metadata",
          data: { thread_id: THREAD_ID, run_id: RUN_ID, turn_index: 1 },
        },
        {
          event: "tool_calls",
          data: {
            agent: "main",
            id: "m-tc",
            tool_calls: [{ id: "tc-send-2", name: "send_message", args: { text: origText } }],
          },
        },
        {
          event: "message_chunk",
          data: {
            agent: "main",
            id: "m-ack",
            content_type: "text",
            content: "已完成",
            finish_reason: "stop",
          },
        },
        {
          event: "run_end",
          data: {
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            outcome: "completed",
          },
        },
      ];
      vi.mocked(hAck.backend.createThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(ackEvents),
      });
      vi.mocked(hAck.backend.appendThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(ackEvents),
      });

      await hAck.app.orchestrator.handleInboundEvent({
        platform: "feishu",
        messageId: "om_r4_imgfix_ack",
        platformUserId: "ou_alice",
        chatType: "dm",
        chatId: "oc_dm_alice",
        chatAddress: "feishu:@me",
        threadScopeKey: "feishu:dm:ou_alice",
        text: "把图发给我",
      });

      expect(
        hAck.backend.deliveryClaims.some(
          (c) => c.delivery_key === `fallback:${RUN_ID}:imgfix`,
        ),
      ).toBe(false);
      expect(hAck.feishuAdapter.sentTexts).toHaveLength(0);
    } finally {
      await hAck.cleanup();
    }
  });

  it("replay entry delivers :imgfix from original agent_send when fallback text is empty", async () => {
    const h = createTestHarness();
    try {
      const origText = "复盘图表：\n![图](charts/replay_raw.png)";
      vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([
          {
            event: "user_message",
            data: {
              turn_index: 1,
              run_id: RUN_ID,
              run_completed_at: "2026-11-27T12:00:00Z",
              content: "发图给我",
            },
          },
          {
            event: "tool_calls",
            data: {
              agent: "main",
              turn_index: 1,
              response_id: RUN_ID,
              id: "m-tc",
              tool_calls: [{ id: "tc-send-r", name: "send_message", args: { text: origText } }],
            },
          },
          {
            event: "replay_done",
            data: { thread_id: THREAD_ID, latest_run_id: RUN_ID, has_more: false },
          },
        ]),
      });
      vi.mocked(h.backend.updateInboundProgress).mockResolvedValue({
        ok: true,
        run_id: RUN_ID,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
        sent_to_current_count: 1,
        has_delivered_current_agent_send: true,
        has_failed_current_agent_send: false,
        has_unresolved_current_agent_send_images: true,
        unresolved_current_agent_sends: [
          {
            delivery_key: `${RUN_ID}:tc-send-r`,
            delivered_text: origText,
            unresolved_image_paths: ["charts/replay_raw.png"],
          },
        ],
        resolved_run_images: {
          "charts/replay_raw.png": "https://cdn.example/replay_resolved.png",
        },
        strict_deliverable_text_enabled: true,
      });

      const orch = h.app.orchestrator as unknown as {
        deliverReplayFallback: (p: Record<string, unknown>) => Promise<boolean>;
      };
      const ok = await orch.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "om_r4_replay_imgfix",
        scopeKey: "feishu:dm:ou_alice",
        generation: 1,
      });
      expect(ok).toBe(true);
      expect(
        h.backend.deliveryClaims.some(
          (c) => c.delivery_key === `fallback:${RUN_ID}:imgfix`,
        ),
      ).toBe(true);
      expect(h.feishuAdapter.sentTexts).toHaveLength(1);
      expect(h.feishuAdapter.sentTexts[0]?.text).toBe(
        "复盘图表：\n![图](https://cdn.example/replay_resolved.png)",
      );
    } finally {
      await h.cleanup();
    }
  });
});

describe("R5 fixes: named tool_calls + MALFORMED preserves text, per-delivery two-version image :imgfix", () => {
  it("preserves truncated prefix through live SSE when Antigravity emits named tool_calls followed by MALFORMED_FUNCTION_CALL and continuation succeeds", async () => {
    const h = createTestHarness();
    try {
      const liveEvents = [
        {
          event: "metadata",
          data: { thread_id: THREAD_ID, run_id: RUN_ID, turn_index: 1 },
        },
        {
          event: "message_chunk",
          data: {
            agent: "main",
            id: "lc_run--0",
            content_type: "text",
            content: "前半段",
            finish_reason: "MAX_TOKENS",
          },
        },
        {
          event: "tool_calls",
          data: {
            agent: "main",
            id: "lc_run--bad",
            tool_calls: [
              { id: "antigravity_call_1", name: "Write", args: { file_path: "a.py" } },
            ],
          },
        },
        {
          event: "message_chunk",
          data: {
            agent: "main",
            id: "lc_run--bad",
            finish_reason: "MALFORMED_FUNCTION_CALL",
          },
        },
        {
          event: "tool_call_result",
          data: {
            agent: "main",
            id: "lc_run--bad-discarded-antigravity_call_1",
            tool_call_id: "antigravity_call_1",
            status: "error",
            discarded: true,
            discarded_reason: "malformed_call",
            content: "not executed",
          },
        },
        {
          event: "message_chunk",
          data: {
            agent: "main",
            id: "lc_run--2",
            continued_from_message_id: "lc_run--0",
            content_type: "text",
            content: "后半段",
            finish_reason: "stop",
          },
        },
        {
          event: "run_end",
          data: { thread_id: THREAD_ID, run_id: RUN_ID, outcome: "completed" },
        },
      ];
      vi.mocked(h.backend.createThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(liveEvents),
      });
      vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(liveEvents),
      });

      await h.app.orchestrator.handleInboundEvent({
        platform: "feishu",
        messageId: "om_r5_malformed_cont",
        platformUserId: "ou_alice",
        chatType: "dm",
        chatId: "oc_dm_alice",
        chatAddress: "feishu:@me",
        threadScopeKey: "feishu:dm:ou_alice",
        text: "请输出完整分析",
      });

      expect(h.feishuAdapter.sentTexts).toHaveLength(1);
      expect(h.feishuAdapter.sentTexts[0]?.text).toBe("前半段后半段");
    } finally {
      await h.cleanup();
    }
  });

  it("records source_message_id and captured_images on agent_send delivery and isolates two versions of the same sandbox image path in live & replay :imgfix", async () => {
    // 1. Verify DeliveryService persists source_message_id + captured_images on first_response_json
    const hDeliv = createTestHarness();
    try {
      const ds = new DeliveryService({
        backend: hDeliv.backend,
        adapters: hDeliv.adapters,
        webBaseUrl: "https://web.example",
      });
      await ds.executeDelivery({
        deliveryKey: `${RUN_ID}:tc-v1`,
        kind: "agent_send",
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        platform: "feishu",
        target: "feishu:@me",
        text: "第一版：\n![图](charts/price.png)",
        sourceMessageId: "msg-v1",
        capturedImages: { "charts/price.png": "https://cdn.example/v1.png" },
      });
      const progressCalls = vi.mocked(hDeliv.backend.updateDeliveryProgress).mock.calls;
      const recorded = progressCalls
        .map(([req]) => req)
        .find((r) => r.delivery_key === `${RUN_ID}:tc-v1` && r.status === "sent");
      expect(recorded?.first_response_json).toMatchObject({
        unresolved_sandbox_images: true,
        unresolved_sandbox_image_paths: ["charts/price.png"],
        delivered_text: "第一版：\n![图](charts/price.png)",
        source_message_id: "msg-v1",
        captured_images: { "charts/price.png": "https://cdn.example/v1.png" },
      });
    } finally {
      await hDeliv.cleanup();
    }

    // 2. Live SSE: same path charts/price.png rewritten from v1 -> v2 in the same turn.
    //    First :imgfix MUST use v1.png (not run_end.images's global v2.png!), second gets v2.png.
    const hLive = createTestHarness();
    try {
      vi.mocked(hLive.backend.updateInboundProgress).mockResolvedValue({
        ok: true,
        run_id: RUN_ID,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
        sent_to_current_count: 2,
        has_delivered_current_agent_send: true,
        has_failed_current_agent_send: false,
        has_unresolved_current_agent_send_images: true,
        unresolved_current_agent_sends: [
          {
            delivery_key: `${RUN_ID}:tc-v1`,
            delivered_text: "第一版：\n![图](charts/price.png)",
            unresolved_image_paths: ["charts/price.png"],
            source_message_id: "msg-v1",
          },
          {
            delivery_key: `${RUN_ID}:tc-v2`,
            delivered_text: "第二版：\n![图](charts/price.png)",
            unresolved_image_paths: ["charts/price.png"],
            source_message_id: "msg-v2",
          },
        ],
        resolved_run_images: {
          "charts/price.png": "https://cdn.example/v2.png",
        },
        strict_deliverable_text_enabled: true,
      });
      const liveEvents = [
        {
          event: "metadata",
          data: { thread_id: THREAD_ID, run_id: RUN_ID, turn_index: 1 },
        },
        {
          event: "tool_calls",
          data: {
            agent: "main",
            id: "msg-v1",
            tool_calls: [
              { id: "tc-v1", name: "send_message", args: { text: "第一版：\n![图](charts/price.png)" } },
            ],
          },
        },
        {
          event: "tool_call_result",
          data: { agent: "main", tool_call_id: "tc-v1", content: "sent" },
        },
        {
          event: "tool_calls",
          data: {
            agent: "main",
            id: "msg-v2",
            tool_calls: [
              { id: "tc-v2", name: "send_message", args: { text: "第二版：\n![图](charts/price.png)" } },
            ],
          },
        },
        {
          event: "tool_call_result",
          data: { agent: "main", tool_call_id: "tc-v2", content: "sent" },
        },
        {
          event: "run_end",
          data: {
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            outcome: "completed",
            images: { "charts/price.png": "https://cdn.example/v2.png" },
            images_by_message: {
              "msg-v1": { "charts/price.png": "https://cdn.example/v1.png" },
              "msg-v2": { "charts/price.png": "https://cdn.example/v2.png" },
            },
          },
        },
      ];
      vi.mocked(hLive.backend.createThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(liveEvents),
      });
      vi.mocked(hLive.backend.appendThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(liveEvents),
      });

      await hLive.app.orchestrator.handleInboundEvent({
        platform: "feishu",
        messageId: "om_r5_two_versions_live",
        platformUserId: "ou_alice",
        chatType: "dm",
        chatId: "oc_dm_alice",
        chatAddress: "feishu:@me",
        threadScopeKey: "feishu:dm:ou_alice",
        text: "分两版把图发给我",
      });

      expect(hLive.feishuAdapter.sentTexts.map((s) => s.text)).toEqual([
        "第一版：\n![图](https://cdn.example/v1.png)",
        "第二版：\n![图](https://cdn.example/v2.png)",
      ]);
    } finally {
      await hLive.cleanup();
    }

    // 3. Replay :imgfix uses I-7 per-delivery/per-message capture (v1), NEVER global v2.
    const hReplay = createTestHarness();
    try {
      vi.mocked(hReplay.backend.replayThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([
          {
            event: "user_message",
            data: {
              turn_index: 1,
              run_id: RUN_ID,
              run_completed_at: "2026-11-27T12:00:00Z",
              content: "分两版把图发给我",
            },
          },
          {
            event: "tool_calls",
            data: {
              agent: "main",
              turn_index: 1,
              response_id: RUN_ID,
              id: "msg-v1",
              tool_calls: [{ id: "tc-v1", name: "send_message", args: { text: "第一版：\n![图](charts/price.png)" } }],
            },
          },
          {
            event: "tool_call_result",
            data: { agent: "main", turn_index: 1, response_id: RUN_ID, tool_call_id: "tc-v1", content: "sent" },
          },
          {
            event: "replay_done",
            data: { thread_id: THREAD_ID, latest_run_id: RUN_ID, has_more: false },
          },
        ]),
      });
      vi.mocked(hReplay.backend.updateInboundProgress).mockResolvedValue({
        ok: true,
        run_id: RUN_ID,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
        sent_to_current_count: 1,
        has_delivered_current_agent_send: true,
        has_failed_current_agent_send: false,
        has_unresolved_current_agent_send_images: true,
        unresolved_current_agent_sends: [
          {
            delivery_key: `${RUN_ID}:tc-v1`,
            delivered_text: "第一版：\n![图](charts/price.png)",
            unresolved_image_paths: ["charts/price.png"],
            source_message_id: "msg-v1",
            captured_images: { "charts/price.png": "https://cdn.example/v1.png" },
          },
        ],
        resolved_run_images: {
          "charts/price.png": "https://cdn.example/v2.png",
        },
        resolved_run_images_by_message: {
          "msg-v1": { "charts/price.png": "https://cdn.example/v1.png" },
          "msg-v2": { "charts/price.png": "https://cdn.example/v2.png" },
        },
        strict_deliverable_text_enabled: true,
      });

      const orch = hReplay.app.orchestrator as unknown as {
        deliverReplayFallback: (p: Record<string, unknown>) => Promise<boolean>;
      };
      const ok = await orch.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "om_r5_two_versions_replay",
        scopeKey: "feishu:dm:ou_alice",
        generation: 1,
      });
      expect(ok).toBe(true);
      expect(hReplay.feishuAdapter.sentTexts).toHaveLength(1);
      expect(hReplay.feishuAdapter.sentTexts[0]?.text).toBe(
        "第一版：\n![图](https://cdn.example/v1.png)",
      );
    } finally {
      await hReplay.cleanup();
    }

    // 4. Isolation: if msg-v1 had no capture of charts/price.png while msg-v2 overwrote it to v2.png,
    //    resolveDeliveryImageUrls MUST NOT leak msg-v2's v2.png into msg-v1's :imgfix.
    expect(
      resolveDeliveryImageUrls({
        deliveryKey: `${RUN_ID}:tc-v1`,
        runId: RUN_ID,
        sourceMessageId: "msg-v1",
        capturedImages: {},
        imagesByMessage: {
          "msg-v2": { "charts/price.png": "https://cdn.example/v2.png" },
        },
        globalImages: {
          "charts/price.png": "https://cdn.example/v2.png",
        },
      }),
    ).toEqual({});
  });
});
