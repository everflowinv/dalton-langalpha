import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { webOnlyApprovalText } from "../src/orchestrator.js";
import type { InboundChannelEvent } from "../src/types.js";
import {
  createTestHarness,
  makeSseEvents,
  RUN_ID,
  type TestHarness,
  THREAD_ID,
} from "./helpers.js";

function makeDmEvent(messageId: string): InboundChannelEvent {
  return {
    platform: "feishu",
    platformUserId: "ou_alice",
    chatType: "dm",
    chatAddress: "feishu:@me",
    chatId: "oc_dm_1",
    threadScopeKey: "dm:ou_alice",
    messageId,
    text: "Buy 10 shares of AAPL",
  };
}

describe("web-only approval prompt", () => {
  it("names the action in Chinese and links the thread", () => {
    expect(webOnlyApprovalText("delete_workspace", "https://x/chat/t/1")).toBe(
      "「删除工作区」需要在网页上确认，请打开：https://x/chat/t/1",
    );
  });

  it("maps order and trading actions to Chinese labels", () => {
    const link = "https://x/chat/t/1";
    expect(webOnlyApprovalText("place_order", link)).toBe(
      "「下单」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(webOnlyApprovalText("submit_order", link)).toBe(
      "「下单」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(webOnlyApprovalText("place_equity_order", link)).toBe(
      "「下单」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(webOnlyApprovalText("ibkr__submit_order", link)).toBe(
      "「下单」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(webOnlyApprovalText("modify_order", link)).toBe(
      "「改单」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(webOnlyApprovalText("replace_order", link)).toBe(
      "「改单」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(webOnlyApprovalText("cancel_order", link)).toBe(
      "「撤单」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(webOnlyApprovalText("cancel_equity_order", link)).toBe(
      "「撤单」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(webOnlyApprovalText("exercise_option", link)).toBe(
      "「期权行权」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(webOnlyApprovalText("order_approval", link)).toBe(
      "「订单操作」需要在网页上确认，请打开：https://x/chat/t/1",
    );
    expect(
      webOnlyApprovalText("order_approval", link, [
        { name: "place_equity_order" },
      ]),
    ).toBe("「下单」需要在网页上确认，请打开：https://x/chat/t/1");
    expect(
      webOnlyApprovalText("order_approval", link, [
        { name: "place_equity_order" },
        { name: "cancel_equity_order" },
      ]),
    ).toBe("「订单操作」需要在网页上确认，请打开：https://x/chat/t/1");
    expect(
      webOnlyApprovalText("order_approval", link, [
        { name: "custom_broker_order_tool" },
      ]),
    ).toBe("「订单操作」需要在网页上确认，请打开：https://x/chat/t/1");
  });

  it("falls back to a neutral name for an action it does not know", () => {
    expect(
      webOnlyApprovalText("unknown_custom_action", "https://x/chat/t/1"),
    ).toBe("「一项操作」需要在网页上确认，请打开：https://x/chat/t/1");
    expect(webOnlyApprovalText(undefined, "https://x/chat/t/1")).toContain(
      "一项操作",
    );
  });
});

describe("real-shape order_approval SSE interrupt prompts", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("resolves 下单, 改单, 撤单, and fallback 订单操作 from real order_governance interrupt payloads", async () => {
    const cases: Array<{
      messageId: string;
      interruptId: string;
      payload: Record<string, unknown>;
      expectedPrefix: string;
    }> = [
      {
        messageId: "m_ord_place",
        interruptId: "int_ord_place",
        payload: {
          thread_id: THREAD_ID,
          run_id: RUN_ID,
          interrupt_id: "int_ord_place",
          kind: "order_approval",
          action_requests: [
            {
              name: "place_equity_order",
              args: { symbol: "AAPL", side: "buy", quantity: 10 },
              description: "Buy 10 shares of AAPL",
              tool_call_id: "tc_ord_1",
              attempt_id: "att_1",
              order: { symbol: "AAPL", side: "buy", quantity: 10 },
            },
          ],
          review_configs: [
            {
              action_name: "place_equity_order",
              allowed_decisions: ["approve", "reject"],
            },
          ],
        },
        expectedPrefix: "「下单」需要在网页上确认，请打开：",
      },
      {
        messageId: "m_ord_modify",
        interruptId: "int_ord_modify",
        payload: {
          thread_id: THREAD_ID,
          run_id: "run_ord_modify",
          interrupt_id: "int_ord_modify",
          kind: "order_approval",
          action_requests: [
            {
              name: "modify_order",
              args: { order_id: "ord_1", limit_price: 195 },
              description: "Modify limit price to 195",
              tool_call_id: "tc_ord_2",
            },
          ],
          review_configs: [
            {
              action_name: "modify_order",
              allowed_decisions: ["approve", "reject"],
            },
          ],
        },
        expectedPrefix: "「改单」需要在网页上确认，请打开：",
      },
      {
        messageId: "m_ord_cancel",
        interruptId: "int_ord_cancel",
        payload: {
          thread_id: THREAD_ID,
          run_id: "run_ord_cancel",
          interrupt_id: "int_ord_cancel",
          kind: "order_approval",
          action_requests: [],
          review_configs: [
            {
              action_name: "cancel_order",
              allowed_decisions: ["approve", "reject"],
            },
          ],
        },
        expectedPrefix: "「撤单」需要在网页上确认，请打开：",
      },
      {
        messageId: "m_ord_unknown",
        interruptId: "int_ord_unknown",
        payload: {
          thread_id: THREAD_ID,
          run_id: "run_ord_unknown",
          interrupt_id: "int_ord_unknown",
          kind: "order_approval",
          action_requests: [
            {
              name: "broker_special_rebalance",
              args: {},
              description: "Special rebalance",
            },
          ],
        },
        expectedPrefix: "「订单操作」需要在网页上确认，请打开：",
      },
    ];

    for (const tc of cases) {
      vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEvents([
          {
            event: "metadata",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: String(tc.payload.run_id),
              turn_index: 1,
            }),
          },
          {
            event: "interrupt",
            data: JSON.stringify(tc.payload),
          },
        ]),
      });

      await h.feishuAdapter.emitInbound(makeDmEvent(tc.messageId));
      const delivered = h.feishuAdapter.sentTexts.at(-1)?.text ?? "";
      expect(delivered).toContain(tc.expectedPrefix);
      expect(delivered).toContain(THREAD_ID);

      const feedback = h.feishuAdapter.feedbackRecords.find(
        (r) => r.messageId === tc.messageId,
      );
      expect(feedback).toMatchObject({
        messageId: tc.messageId,
        settled: true,
        outcome: "interrupted",
      });
    }
  });
});
