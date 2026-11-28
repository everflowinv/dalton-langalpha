import { describe, expect, it } from "vitest";
import {
  buildHtmlWidgetDeepLink,
  extractRunChartsAndWidgetsFromReplay,
  resolveHtmlWidgetDeepLinkId,
  type CollectedChartLayerRef,
  type CollectedHtmlWidgetRef,
} from "../src/chart-snapshots.js";
import { ChannelGatewayOrchestrator } from "../src/orchestrator.js";

describe("html_widget deep-link identity", () => {
  it("prefers payload.widget_id over event artifact_id (tool call id) and falls back when absent", () => {
    expect(
      resolveHtmlWidgetDeepLinkId(
        { artifact_id: "call_123" },
        { widget_id: "w_persistent_9", title: "Valuation" },
      ),
    ).toBe("w_persistent_9");

    expect(
      resolveHtmlWidgetDeepLinkId(
        { artifact_id: "call_123" },
        { widget_id: "   ", title: "Valuation" },
      ),
    ).toBe("call_123");

    expect(
      resolveHtmlWidgetDeepLinkId(
        {},
        { artifact_id: "legacy_inner_id", title: "Valuation" },
      ),
    ).toBe("legacy_inner_id");
  });

  it("extracts payload.widget_id for ?widget= deep links in replay artifacts", async () => {
    const runId = "run-widget-id-1";
    const res = await extractRunChartsAndWidgetsFromReplay({
      backend: {
        replayThreadMessages: async () => ({
          status: 200,
          isSse: true,
          events: (async function* () {})(),
        }),
        replayThreadRun: async () => ({
          status: 200,
          isSse: true,
          events: (async function* () {
            yield {
              event: "user_message",
              data: JSON.stringify({ turn_index: 1, run_id: runId }),
            };
            yield {
              event: "artifact",
              data: JSON.stringify({
                artifact_type: "html_widget",
                artifact_id: "call_show_widget_1",
                response_id: runId,
                payload: {
                  widget_id: "w_dcf_table_1",
                  title: "DCF Table",
                },
              }),
            };
            yield {
              event: "artifact",
              data: JSON.stringify({
                artifact_type: "html_widget",
                artifact_id: "call_legacy_2",
                response_id: runId,
                payload: {
                  title: "Legacy Widget",
                },
              }),
            };
            yield {
              event: "replay_done",
              data: JSON.stringify({ thread_id: "th-1" }),
            };
          })(),
        }),
      },
      userId: "u-1",
      threadId: "th-1",
      targetRunId: runId,
    });

    expect(res.found).toBe(true);
    expect(res.widgets).toEqual([
      { artifactId: "w_dcf_table_1", title: "DCF Table", monitorSlug: null },
      { artifactId: "call_legacy_2", title: "Legacy Widget", monitorSlug: null },
    ]);
    expect(
      buildHtmlWidgetDeepLink("https://app.example.com", "th-1", res.widgets[0].artifactId),
    ).toBe("https://app.example.com/chat/t/th-1?widget=w_dcf_table_1");
  });

  it("uses payload.widget_id in live orchestrator artifact collection too", () => {
    const chartsMap = new Map<string, CollectedChartLayerRef>();
    const widgetsMap = new Map<string, CollectedHtmlWidgetRef>();
    const collect = (
      ChannelGatewayOrchestrator.prototype as unknown as {
        collectArtifactChartOrWidget: (
          rawPayload: Record<string, unknown>,
          charts: Map<string, CollectedChartLayerRef>,
          widgets: Map<string, CollectedHtmlWidgetRef>,
        ) => void;
      }
    ).collectArtifactChartOrWidget;

    collect(
      {
        artifact_type: "html_widget",
        artifact_id: "call_live_1",
        payload: {
          widget_id: "w_live_42",
          title: "Live Widget",
        },
      },
      chartsMap,
      widgetsMap,
    );

    expect(Array.from(widgetsMap.values())).toEqual([
      { artifactId: "w_live_42", title: "Live Widget", monitorSlug: null },
    ]);
    expect(widgetsMap.has("call_live_1")).toBe(false);
  });
});
