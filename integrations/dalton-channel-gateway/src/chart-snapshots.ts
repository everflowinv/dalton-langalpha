/**
 * Channel Phase 2 Batch 2: System chart_layer DM snapshot delivery & web interactive deep links.
 *
 * Implements §2.3 and Lead Pre-Dev Decisions #1 & #2:
 * - Collects `chart_layer` (deduplicated by `layer_id`) and `html_widget` (`ShowWidget`) artifacts
 *   from live SSE streams, replay streams, and background command/wakeup streams.
 * - Polls backend `GET /internal/snapshots/status` (Postgres authoritative) for up to 20s when
 *   DM chart snapshots are still `pending`. Transitions to PNG image delivery once `ready`;
 *   falls back cleanly to web interactive deep links on 20s timeout, `failed`, crashed render
 *   worker, outbound-blocked run, group chat, or `html_widget`.
 * - Delivers via Feishu Schema 2.0 card (`__img__` + `open_url` button), Discord multipart PNG
 *   with ActionRow Link Button (`style: 5`), and Weixin iLink encrypted `IMAGE` + deep link text.
 */

import {
  BackendHttpError,
  type BackendClient,
  type ChartSnapshotItem,
  type DeliveryActivityOrigin,
  type I6AuthorizeAndClaimResponse,
  type WeixinPreflightAuthorizedData,
} from "./backend-client.js";
import {
  ChannelDeliveryError,
  type ChannelAdapter,
  type SendCardImageInput,
} from "./channels/adapter.js";
import { buildFeishuChartSnapshotCard } from "./channels/feishu/format.js";
import { isStaleGatewayEpochError } from "./gateway-lease.js";
import type { ChannelPlatform } from "./types.js";

export const DEFAULT_CHART_SNAPSHOT_WAIT_TIMEOUT_MS = 20_000;
export const DEFAULT_CHART_SNAPSHOT_POLL_INTERVAL_MS = 1_000;
export const FEISHU_CHART_IMAGE_MAX_BYTES = 10 * 1024 * 1024; // 10 MB Feishu inline image cap
export const MAX_CHARTS_PER_TURN = 4;

export interface CollectedChartLayerRef {
  chartId: string;
  layerId: string;
  symbol?: string | null;
  timeframe?: string | null;
  title?: string | null;
  workspaceId?: string | null;
}

export interface CollectedHtmlWidgetRef {
  artifactId: string;
  title?: string | null;
  monitorSlug?: string | null;
}

/**
 * Canonical composite key `(chart_id, layer_id)` for deduplicating chart layers and matching
 * persisted snapshots across charts that reuse the same `layer_id`.
 */
export function chartLayerCompositeKey(chartId: string, layerId: string): string {
  return `${chartId.trim()}\0${layerId.trim()}`;
}

/**
 * Collect persisted `PublishedLayer[]` items (from `tool_call_result.artifact.chart_layers`,
 * `run_end.chart_layers`, or `metadata`/`user_message.metadata.report_source.chart_layers`)
 * into `chartsMap` keyed by `(chartId, layerId)`.
 */
export function collectPublishedChartLayers(
  rawLayers: unknown,
  chartsMap: Map<string, CollectedChartLayerRef>,
): void {
  if (!Array.isArray(rawLayers)) return;
  for (const entry of rawLayers) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
    const rec = entry as Record<string, unknown>;
    const layerId =
      typeof rec.layer_id === "string"
        ? rec.layer_id.trim()
        : typeof rec.id === "string"
          ? rec.id.trim()
          : "";
    const chartId =
      typeof rec.chart_id === "string" ? rec.chart_id.trim() : "";
    if (!layerId || !chartId) continue;
    const title =
      typeof rec.title === "string" && rec.title.trim()
        ? rec.title.trim()
        : chartId;
    chartsMap.set(chartLayerCompositeKey(chartId, layerId), {
      chartId,
      layerId,
      symbol: typeof rec.symbol === "string" ? rec.symbol : null,
      timeframe: typeof rec.timeframe === "string" ? rec.timeframe : null,
      title,
      workspaceId:
        typeof rec.workspace_id === "string" ? rec.workspace_id : null,
    });
  }
}

/**
 * Collect `chart_layers` from `user_message.metadata` (`report_source.chart_layers` and
 * `command_reports[*].chart_layers`).
 */
export function collectReportSourceChartLayers(
  metadata: unknown,
  chartsMap: Map<string, CollectedChartLayerRef>,
): void {
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) {
    return;
  }
  const meta = metadata as Record<string, unknown>;
  const reportSource = meta.report_source;
  if (
    reportSource &&
    typeof reportSource === "object" &&
    !Array.isArray(reportSource)
  ) {
    collectPublishedChartLayers(
      (reportSource as Record<string, unknown>).chart_layers,
      chartsMap,
    );
  }
  const cmdReports = meta.command_reports;
  if (Array.isArray(cmdReports)) {
    for (const item of cmdReports) {
      if (item && typeof item === "object" && !Array.isArray(item)) {
        collectPublishedChartLayers(
          (item as Record<string, unknown>).chart_layers,
          chartsMap,
        );
      }
    }
  }
}

/**
 * Extract charts (from `artifact`, `tool_call_result.artifact.chart_layers`, and
 * `user_message.metadata.report_source.chart_layers`), persisted `chart_snapshots`, and
 * `html_widget` artifacts for `targetRunId` via direct run-scoped replay (`replayThreadRun`,
 * avoiding any 50-page / 500-turn ceiling) with paginated `/messages/replay` fallback when
 * `replayThreadRun` is unavailable.
 */
export async function extractRunChartsAndWidgetsFromReplay(params: {
  backend: Pick<BackendClient, "replayThreadMessages"> &
    Partial<Pick<BackendClient, "replayThreadRun">>;
  userId: string;
  threadId: string;
  targetRunId: string;
  executionId?: string | null;
  pageSize?: number;
  maxPages?: number;
}): Promise<{
  found: boolean;
  charts: CollectedChartLayerRef[];
  snapshots: ChartSnapshotItem[];
  widgets: CollectedHtmlWidgetRef[];
  outboundBlocked?: boolean;
  retryable?: boolean;
  httpStatus?: number | null;
}> {
  const chartsMap = new Map<string, CollectedChartLayerRef>();
  const widgetsMap = new Map<string, CollectedHtmlWidgetRef>();
  let snapshots: ChartSnapshotItem[] = [];
  let outboundBlocked: boolean | undefined;

  const consumeSseStream = async (
    events: AsyncIterable<{ event: string; data: string }>,
  ): Promise<{
    foundTargetRunInPage: boolean;
    sawReplayDone: boolean;
    sawTargetPlaceholderOnly: boolean;
    pageHasMore: boolean;
    pageFirstTurnIndex: number | null;
    earliestTurnInPage: number | null;
  }> => {
    let currentRunId: string | null = null;
    let sawTargetStubHeader = false;
    let sawTargetPageEvent = false;
    let sawTargetReplayUnavailable = false;
    let sawTargetActiveInSnapshot = false;
    let sawReplayDone = false;
    let pageHasMore = false;
    let pageFirstTurnIndex: number | null = null;
    let earliestTurnInPage: number | null = null;

    for await (const sse of events) {
      if (sse.event === ":comment") continue;
      let data: Record<string, unknown>;
      try {
        const parsed = JSON.parse(sse.data);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) continue;
        data = parsed as Record<string, unknown>;
      } catch {
        continue;
      }

      if (sse.event === "history_page") {
        if (typeof data.has_more === "boolean") {
          pageHasMore = data.has_more;
        }
        if (typeof data.first_turn_index === "number") {
          pageFirstTurnIndex = data.first_turn_index;
        } else if (typeof data.oldest_turn_index === "number") {
          pageFirstTurnIndex = data.oldest_turn_index;
        }
        continue;
      }

      if (typeof data.turn_index === "number") {
        if (earliestTurnInPage === null || data.turn_index < earliestTurnInPage) {
          earliestTurnInPage = data.turn_index;
        }
      }

      if (
        sse.event === "user_message" ||
        sse.event === "turn_separator" ||
        sse.event === "turn_start"
      ) {
        const terminalEvtRunId =
          typeof data.response_id === "string" && data.response_id.trim()
            ? data.response_id.trim()
            : typeof data.run_id === "string" && data.run_id.trim()
              ? data.run_id.trim()
              : null;
        const stubReportRunId =
          typeof data.report_run_id === "string" && data.report_run_id.trim()
            ? data.report_run_id.trim()
            : null;
        const evtRunId = terminalEvtRunId ?? stubReportRunId;
        if (evtRunId) {
          currentRunId = evtRunId;
        }
        if (terminalEvtRunId === params.targetRunId) {
          // `items.py:_user_message_item` stamps `run_id` only once the turn is terminal.
          sawTargetPageEvent = true;
          if (sse.event === "user_message") {
            collectReportSourceChartLayers(data.metadata, chartsMap);
          }
        } else if (stubReportRunId === params.targetRunId) {
          // `report_run_id` alone marks a system/in-progress stub header; wait for
          // actual non-placeholder turn events before considering the turn found.
          sawTargetStubHeader = true;
          if (sse.event === "user_message") {
            collectReportSourceChartLayers(data.metadata, chartsMap);
          }
        }
        continue;
      }

      if (sse.event === "snapshot") {
        const activeRuns = Array.isArray(data.active_runs) ? data.active_runs : [];
        for (const item of activeRuns) {
          if (
            item &&
            typeof item === "object" &&
            !Array.isArray(item) &&
            typeof (item as Record<string, unknown>).run_id === "string" &&
            ((item as Record<string, unknown>).run_id as string).trim() ===
              params.targetRunId
          ) {
            sawTargetActiveInSnapshot = true;
          }
        }
        continue;
      }

      const ownRunId =
        typeof data.response_id === "string" && data.response_id.trim()
          ? data.response_id.trim()
          : typeof data.run_id === "string" && data.run_id.trim()
            ? data.run_id.trim()
            : null;
      const rId = ownRunId ?? currentRunId;

      if (sse.event === "error") {
        const errType =
          typeof data.type === "string" ? data.type.trim().toLowerCase() : "";
        if (
          errType === "replay_unavailable" &&
          (rId === params.targetRunId || rId === null)
        ) {
          sawTargetReplayUnavailable = true;
        }
        continue;
      }

      if (sse.event === "replay_done") {
        sawReplayDone = true;
        // BW2: NEVER derive `found` from `replay_done.run_id`; `found` requires
        // actual target turn events in the page AND this complete closing frame.
        if (typeof data.has_more === "boolean") {
          pageHasMore = data.has_more;
        }
        if (pageFirstTurnIndex === null) {
          if (typeof data.oldest_turn_index === "number") {
            pageFirstTurnIndex = data.oldest_turn_index;
          } else if (typeof data.first_turn_index === "number") {
            pageFirstTurnIndex = data.first_turn_index;
          }
        }
        const rawSnapsByRun = data.chart_snapshots_by_run;
        if (
          rawSnapsByRun &&
          typeof rawSnapsByRun === "object" &&
          !Array.isArray(rawSnapsByRun)
        ) {
          const runSnaps = (rawSnapsByRun as Record<string, unknown>)[
            params.targetRunId
          ];
          const parsedSnaps = parseChartSnapshotsList(runSnaps);
          if (parsedSnaps.length > 0) {
            snapshots = parsedSnaps;
          }
        }
        const rawBlockedByRun = data.outbound_blocked_by_run;
        if (
          rawBlockedByRun &&
          typeof rawBlockedByRun === "object" &&
          !Array.isArray(rawBlockedByRun)
        ) {
          const rawBlocked = (rawBlockedByRun as Record<string, unknown>)[
            params.targetRunId
          ];
          if (typeof rawBlocked === "boolean") {
            outboundBlocked = rawBlocked;
          }
        }
        continue;
      }

      if (rId === params.targetRunId) {
        sawTargetPageEvent = true;
      }

      if (sse.event === "tool_call_result" && rId === params.targetRunId) {
        const art = data.artifact;
        if (art && typeof art === "object" && !Array.isArray(art)) {
          collectPublishedChartLayers(
            (art as Record<string, unknown>).chart_layers,
            chartsMap,
          );
        }
        continue;
      }

      if (sse.event === "artifact" && rId === params.targetRunId) {
        const artifactType =
          typeof data.artifact_type === "string" ? data.artifact_type.trim() : "";
        const inner =
          data.payload && typeof data.payload === "object" && !Array.isArray(data.payload)
            ? (data.payload as Record<string, unknown>)
            : data;
        if (artifactType === "chart_layer") {
          const layerId =
            typeof inner.layer_id === "string"
              ? inner.layer_id.trim()
              : typeof data.artifact_id === "string"
                ? data.artifact_id.trim()
                : "";
          const chartId =
            typeof inner.chart_id === "string" ? inner.chart_id.trim() : "";
          if (layerId && chartId) {
            chartsMap.set(chartLayerCompositeKey(chartId, layerId), {
              chartId,
              layerId,
              symbol: typeof inner.symbol === "string" ? inner.symbol : null,
              timeframe: typeof inner.timeframe === "string" ? inner.timeframe : null,
              title: typeof inner.title === "string" ? inner.title : chartId,
              workspaceId:
                typeof inner.workspace_id === "string" ? inner.workspace_id : null,
            });
          }
        } else if (artifactType === "html_widget") {
          const artifactId =
            typeof data.artifact_id === "string"
              ? data.artifact_id.trim()
              : typeof inner.widget_id === "string"
                ? inner.widget_id.trim()
                : "";
          if (artifactId) {
            widgetsMap.set(artifactId, {
              artifactId,
              title: typeof inner.title === "string" ? inner.title : artifactId,
              monitorSlug:
                typeof inner.monitor_slug === "string" && inner.monitor_slug.trim()
                  ? inner.monitor_slug.trim()
                  : null,
            });
          }
        }
        continue;
      }
    }

    const sawTargetPlaceholderOnly =
      (sawTargetStubHeader && !sawTargetPageEvent) ||
      sawTargetReplayUnavailable ||
      sawTargetActiveInSnapshot;
    const foundTargetRunInPage =
      sawReplayDone &&
      !sawTargetReplayUnavailable &&
      !sawTargetActiveInSnapshot &&
      sawTargetPageEvent;

    return {
      foundTargetRunInPage,
      sawReplayDone,
      sawTargetPlaceholderOnly,
      pageHasMore,
      pageFirstTurnIndex,
      earliestTurnInPage,
    };
  };

  // BW2: Prefer direct run-scoped replay by run_id (no page ceiling).
  if (typeof params.backend.replayThreadRun === "function") {
    const execId = params.executionId?.trim();
    const resp = execId
      ? await params.backend.replayThreadRun(
          params.userId,
          params.threadId,
          params.targetRunId,
          { executionId: execId },
        )
      : await params.backend.replayThreadRun(
          params.userId,
          params.threadId,
          params.targetRunId,
        );
    if (!resp.isSse || !resp.events) {
      const retryable =
        resp.status === 0 || resp.status === 429 || resp.status >= 500;
      return {
        found: false,
        charts: [],
        snapshots: [],
        widgets: [],
        outboundBlocked: undefined,
        retryable,
        httpStatus: resp.status,
      };
    }
    const { foundTargetRunInPage, sawReplayDone, sawTargetPlaceholderOnly } =
      await consumeSseStream(resp.events);
    return {
      found: foundTargetRunInPage,
      charts: [...chartsMap.values()],
      snapshots,
      widgets: [...widgetsMap.values()],
      outboundBlocked,
      retryable: !foundTargetRunInPage && (!sawReplayDone || sawTargetPlaceholderOnly),
      httpStatus: resp.status,
    };
  }

  // Fallback for older backends / unit stubs that only implement paginated replayThreadMessages.
  const pageSize = Math.max(1, params.pageSize ?? 10);
  const maxPages = Math.max(1, params.maxPages ?? 50);
  let beforeTurn: number | undefined;
  let foundTargetRun = false;
  let sawRetryableFailure = false;
  let lastHttpStatus: number | null = null;

  for (let page = 0; page < maxPages; page += 1) {
    const resp = await params.backend.replayThreadMessages(
      params.userId,
      params.threadId,
      beforeTurn !== undefined
        ? { limit: pageSize, beforeTurn }
        : { limit: pageSize },
    );
    lastHttpStatus = resp.status;
    if (!resp.isSse || !resp.events) {
      if (resp.status === 0 || resp.status === 429 || resp.status >= 500) {
        sawRetryableFailure = true;
      }
      break;
    }

    const {
      foundTargetRunInPage,
      sawReplayDone,
      sawTargetPlaceholderOnly,
      pageHasMore,
      pageFirstTurnIndex,
      earliestTurnInPage,
    } = await consumeSseStream(resp.events);

    if (foundTargetRunInPage) {
      foundTargetRun = true;
      break;
    }
    if (!sawReplayDone || sawTargetPlaceholderOnly) {
      sawRetryableFailure = true;
      if (!sawReplayDone) {
        break;
      }
    }
    if (!pageHasMore) {
      break;
    }
    const nextBeforeTurn = pageFirstTurnIndex ?? earliestTurnInPage;
    if (
      nextBeforeTurn === null ||
      nextBeforeTurn <= 0 ||
      (beforeTurn !== undefined && nextBeforeTurn >= beforeTurn)
    ) {
      break;
    }
    beforeTurn = nextBeforeTurn;
  }

  return {
    found: foundTargetRun,
    charts: [...chartsMap.values()],
    snapshots,
    widgets: [...widgetsMap.values()],
    outboundBlocked,
    retryable: !foundTargetRun && sawRetryableFailure,
    httpStatus: lastHttpStatus,
  };
}

export function isPublicWebBaseUrl(webBaseUrl?: string | null): boolean {
  const cleaned = (webBaseUrl ?? "").trim().replace(/\/+$/, "");
  if (!cleaned) return false;
  if (!/^https?:\/\//i.test(cleaned)) return false;
  if (
    /^https?:\/\/(?:localhost|127\.\d+\.\d+\.\d+|\[::1\]|0\.0\.0\.0)(?:[:/]|$)/i.test(
      cleaned,
    )
  ) {
    return false;
  }
  return true;
}

export function buildChartLayerDeepLink(
  webBaseUrl: string | null | undefined,
  threadId: string | null | undefined,
  chartId: string,
  layerId: string,
): string | null {
  if (!isPublicWebBaseUrl(webBaseUrl)) return null;
  const tid = (threadId ?? "").trim();
  const cid = chartId.trim();
  const lid = layerId.trim();
  if (!tid || !cid || !lid) return null;
  const base = webBaseUrl!.trim().replace(/\/+$/, "");
  return `${base}/chat/t/${encodeURIComponent(tid)}?chart=${encodeURIComponent(cid)}&layer=${encodeURIComponent(lid)}`;
}

export function buildHtmlWidgetDeepLink(
  webBaseUrl: string | null | undefined,
  threadId: string | null | undefined,
  artifactId: string,
  monitorSlug?: string | null,
): string | null {
  if (!isPublicWebBaseUrl(webBaseUrl)) return null;
  const base = webBaseUrl!.trim().replace(/\/+$/, "");
  const slug = (monitorSlug ?? "").trim();
  if (slug) {
    return `${base}/monitors/${encodeURIComponent(slug)}`;
  }
  const tid = (threadId ?? "").trim();
  const aid = artifactId.trim();
  if (!tid || !aid) return null;
  return `${base}/chat/t/${encodeURIComponent(tid)}?widget=${encodeURIComponent(aid)}`;
}

export function buildDiscordLinkButtonRow(
  label: string,
  url: string,
): Array<Record<string, unknown>> {
  return [
    {
      type: 1, // ActionRow
      components: [
        {
          type: 2, // Button
          style: 5, // Link Button
          label,
          url,
        },
      ],
    },
  ];
}

export function isDmTargetAddress(targetAddress: string | null | undefined): boolean {
  const cleaned = (targetAddress ?? "").trim().toLowerCase();
  if (!cleaned) return false;
  if (cleaned.endsWith(":@me")) return true;
  if (/^(?:feishu|discord|weixin|wechat):(?:user|dm):\S+/.test(cleaned)) return true;
  return false;
}

export function parseChartSnapshotsList(raw: unknown): ChartSnapshotItem[] {
  if (!Array.isArray(raw)) return [];
  const out: ChartSnapshotItem[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
    const rec = entry as Record<string, unknown>;
    const chartId = typeof rec.chart_id === "string" ? rec.chart_id.trim() : "";
    const layerId = typeof rec.layer_id === "string" ? rec.layer_id.trim() : "";
    if (!chartId || !layerId) continue;
    const statusRaw = typeof rec.status === "string" ? rec.status.trim().toLowerCase() : "pending";
    const status =
      statusRaw === "ready" || statusRaw === "failed" || statusRaw === "pending"
        ? statusRaw
        : "pending";
    out.push({
      artifact_type:
        typeof rec.artifact_type === "string" ? rec.artifact_type : "chart_layer",
      status,
      workspace_id: typeof rec.workspace_id === "string" ? rec.workspace_id : null,
      chart_id: chartId,
      symbol: typeof rec.symbol === "string" ? rec.symbol : null,
      timeframe: typeof rec.timeframe === "string" ? rec.timeframe : null,
      layer_id: layerId,
      title: typeof rec.title === "string" ? rec.title : null,
      command_id: typeof rec.command_id === "string" ? rec.command_id : null,
      snapshot_key: typeof rec.snapshot_key === "string" ? rec.snapshot_key : null,
      sha256: typeof rec.sha256 === "string" ? rec.sha256 : null,
      size_bytes: typeof rec.size_bytes === "number" ? rec.size_bytes : null,
      width: typeof rec.width === "number" ? rec.width : null,
      height: typeof rec.height === "number" ? rec.height : null,
      error: typeof rec.error === "string" ? rec.error : null,
      pending_since: typeof rec.pending_since === "string" ? rec.pending_since : null,
      updated_at: typeof rec.updated_at === "string" ? rec.updated_at : null,
    });
  }
  return out;
}

export interface WaitForSnapshotsResult {
  snapshotsByKey: Map<string, ChartSnapshotItem>;
  snapshotsByLayerId: Map<string, ChartSnapshotItem>;
  outboundBlocked: boolean;
  statusConfirmed: boolean;
  timedOut: boolean;
}

/**
 * Poll backend `GET /internal/snapshots/status` (Postgres authoritative) while DM chart snapshots
 * remain `pending`, bounded by `timeoutMs` (default 20s, Lead Decision #1).
 */
export async function waitForChartSnapshotsReady(params: {
  backend: Pick<BackendClient, "getChartSnapshotsStatus">;
  runId: string;
  threadId?: string | null;
  platform?: ChannelPlatform | null;
  initialSnapshots?: ChartSnapshotItem[];
  expectedChartKeys?: string[];
  expectedLayerIds?: string[];
  outboundBlocked?: boolean;
  isDm: boolean;
  timeoutMs?: number;
  pollIntervalMs?: number;
  nowFn?: () => number;
  sleepFn?: (ms: number) => Promise<void>;
}): Promise<WaitForSnapshotsResult> {
  const byKey = new Map<string, ChartSnapshotItem>();
  const byLayer = new Map<string, ChartSnapshotItem>();
  for (const snap of params.initialSnapshots ?? []) {
    if (snap.chart_id && snap.layer_id) {
      byKey.set(chartLayerCompositeKey(snap.chart_id, snap.layer_id), snap);
      byLayer.set(snap.layer_id, snap);
    }
  }

  if (params.outboundBlocked === true) {
    return {
      snapshotsByKey: new Map(),
      snapshotsByLayerId: new Map(),
      outboundBlocked: true,
      statusConfirmed: true,
      timedOut: false,
    };
  }

  // Group chats never fetch PNG snapshots; skip polling entirely.
  if (!params.isDm) {
    return {
      snapshotsByKey: byKey,
      snapshotsByLayerId: byLayer,
      outboundBlocked: Boolean(params.outboundBlocked),
      statusConfirmed: typeof params.outboundBlocked === "boolean",
      timedOut: false,
    };
  }

  const expectedComposite = new Set<string>(
    (params.expectedChartKeys ?? []).filter((k) => Boolean(k && k.trim())),
  );
  for (const k of byKey.keys()) {
    expectedComposite.add(k);
  }
  const expectedLegacyLayers = new Set<string>(
    expectedComposite.size === 0
      ? (params.expectedLayerIds ?? []).filter((id) => Boolean(id && id.trim()))
      : [],
  );

  if (expectedComposite.size === 0 && expectedLegacyLayers.size === 0) {
    return {
      snapshotsByKey: byKey,
      snapshotsByLayerId: byLayer,
      outboundBlocked: false,
      statusConfirmed: typeof params.outboundBlocked === "boolean",
      timedOut: false,
    };
  }

  const needsPoll = (): boolean => {
    if (expectedComposite.size > 0) {
      for (const ck of expectedComposite) {
        const entry = byKey.get(ck);
        if (!entry || entry.status === "pending") {
          return true;
        }
      }
      return false;
    }
    for (const lid of expectedLegacyLayers) {
      const entry = byLayer.get(lid);
      if (!entry || entry.status === "pending") {
        return true;
      }
    }
    return false;
  };

  if (!needsPoll()) {
    const hasReadySnapshot = [...byKey.values()].some((s) => s.status === "ready");
    return {
      snapshotsByKey: byKey,
      snapshotsByLayerId: byLayer,
      outboundBlocked: false,
      statusConfirmed: params.outboundBlocked === false || hasReadySnapshot,
      timedOut: false,
    };
  }

  const getStatus = params.backend.getChartSnapshotsStatus?.bind(params.backend);
  if (!getStatus) {
    return {
      snapshotsByKey: byKey,
      snapshotsByLayerId: byLayer,
      outboundBlocked: false,
      statusConfirmed: params.outboundBlocked === false,
      timedOut: false,
    };
  }

  const nowFn = params.nowFn ?? Date.now;
  const sleepFn =
    params.sleepFn ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
  const timeoutMs = Math.max(0, params.timeoutMs ?? DEFAULT_CHART_SNAPSHOT_WAIT_TIMEOUT_MS);
  const pollIntervalMs = Math.max(
    10,
    params.pollIntervalMs ?? DEFAULT_CHART_SNAPSHOT_POLL_INTERVAL_MS,
  );
  const deadline = nowFn() + timeoutMs;
  let statusConfirmed = params.outboundBlocked === false;

  while (true) {
    const remainingBeforeReq = deadline - nowFn();
    if (remainingBeforeReq <= 0) {
      return {
        snapshotsByKey: byKey,
        snapshotsByLayerId: byLayer,
        outboundBlocked: false,
        statusConfirmed,
        timedOut: true,
      };
    }

    const controller = new AbortController();
    let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
    let deadlineAborted = false;
    const abortPromise = new Promise<never>((_resolve, reject) => {
      deadlineTimer = setTimeout(() => {
        deadlineAborted = true;
        const abortErr = new Error("snapshot_poll_deadline_exceeded");
        controller.abort(abortErr);
        reject(abortErr);
      }, Math.max(1, remainingBeforeReq));
    });
    // Prevent unhandled rejection when getStatus finishes before deadlineTimer
    abortPromise.catch(() => undefined);

    try {
      const resp = await Promise.race([
        getStatus({
          runId: params.runId,
          threadId: params.threadId ?? null,
          platform: params.platform ?? null,
          signal: controller.signal,
          timeoutMs: Math.min(10_000, Math.max(1, remainingBeforeReq)),
        }),
        abortPromise,
      ]);
      if (resp.outbound_blocked === true) {
        return {
          snapshotsByKey: new Map(),
          snapshotsByLayerId: new Map(),
          outboundBlocked: true,
          statusConfirmed: true,
          timedOut: false,
        };
      }
      if (resp.outbound_blocked === false) {
        statusConfirmed = true;
      }
      const parsedSnaps = parseChartSnapshotsList(resp.snapshots);
      for (const snap of parsedSnaps) {
        byKey.set(chartLayerCompositeKey(snap.chart_id, snap.layer_id), snap);
        byLayer.set(snap.layer_id, snap);
      }
      // Postgres `/internal/snapshots/status` is authoritative: all eligible snapshots for a run
      // are registered synchronously before `run_end`, so once Postgres reports no pending rows
      // (even when some charts in the turn have no snapshot row), stop polling immediately.
      const responseHasPending =
        resp.has_pending === true ||
        parsedSnaps.some((snap) => snap.status === "pending");
      if (!responseHasPending) {
        return {
          snapshotsByKey: byKey,
          snapshotsByLayerId: byLayer,
          outboundBlocked: false,
          statusConfirmed,
          timedOut: false,
        };
      }
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        throw err;
      }
      if (deadlineAborted || controller.signal.aborted) {
        return {
          snapshotsByKey: byKey,
          snapshotsByLayerId: byLayer,
          outboundBlocked: false,
          statusConfirmed,
          timedOut: true,
        };
      }
      if (err instanceof BackendHttpError && (err.status === 404 || err.status === 403)) {
        return {
          snapshotsByKey: byKey,
          snapshotsByLayerId: byLayer,
          outboundBlocked: false,
          statusConfirmed,
          timedOut: false,
        };
      }
      // Transient error: keep polling until deadline, then degrade to fixed-copy deep link.
    } finally {
      if (deadlineTimer !== undefined) {
        clearTimeout(deadlineTimer);
      }
    }

    const remaining = deadline - nowFn();
    if (remaining <= 0) {
      return {
        snapshotsByKey: byKey,
        snapshotsByLayerId: byLayer,
        outboundBlocked: false,
        statusConfirmed,
        timedOut: true,
      };
    }
    await sleepFn(Math.min(pollIntervalMs, remaining));
  }
}

function buildTextDeepLinkMessage(params: {
  title: string;
  webUrl: string | null;
  isWidget?: boolean;
  fixedCopy?: boolean;
}): string {
  const prefix = params.isWidget ? "交互组件" : "看盘图表";
  if (params.fixedCopy || params.isWidget) {
    if (params.webUrl) {
      return `${prefix}已生成，请登录后在网页端查看：\n${params.webUrl}`;
    }
    return `${prefix}已生成，请登录后在网页端对应会话或工作区查看。`;
  }
  if (params.webUrl) {
    return `${prefix}：${params.title}\n网页交互版：${params.webUrl}`;
  }
  return `${prefix}：${params.title}\n请在网页端对应会话或工作区查看。`;
}

async function resolveWeixinPreflightForChart(
  backend: BackendClient,
  deliveryKey: string,
  deliveryAttempt: number,
  chunkIndex: number,
): Promise<WeixinPreflightAuthorizedData | null> {
  const resp = await backend.preflightWeixinSend(deliveryKey, {
    delivery_attempt: deliveryAttempt,
    chunk_index: chunkIndex,
  });
  if (resp.authorized) {
    return resp;
  }
  return null;
}

export interface PendingChartDeliveryReceipt {
  deliveryKey: string;
  platform: ChannelPlatform;
  deliveryAttempt?: number;
  sentImages: number;
  sentLinks: number;
  totalItems: number;
  platformMessageIds: string[];
  finalStatus: "sent" | "partial" | "failed" | "unknown";
  timedOutPending: boolean;
}

const MAX_PENDING_CHART_RECEIPTS = 500;
const DEFAULT_PENDING_CHART_RECEIPTS = new Map<
  string,
  PendingChartDeliveryReceipt
>();

function rememberPendingChartReceipt(
  store: Map<string, PendingChartDeliveryReceipt>,
  receipt: PendingChartDeliveryReceipt,
): void {
  if (store.has(receipt.deliveryKey)) {
    store.delete(receipt.deliveryKey);
  } else if (store.size >= MAX_PENDING_CHART_RECEIPTS) {
    const oldest = store.keys().next().value;
    if (oldest !== undefined) {
      store.delete(oldest);
    }
  }
  store.set(receipt.deliveryKey, receipt);
}

export interface DeliverChartsAndWidgetsParams {
  backend: BackendClient;
  adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  webBaseUrl?: string | null;
  userId: string;
  threadId: string | null;
  runId: string;
  platform: ChannelPlatform;
  targetAddress: string;
  deliveryKey?: string;
  deliveryKind?: "fallback" | "automation";
  executionId?: string | null;
  outboundBlocked?: boolean;
  inboundMessageId?: string | null;
  threadScopeKey?: string | null;
  sourceScopeKey?: string | null;
  sourceGeneration?: number | null;
  activityOrigin?: DeliveryActivityOrigin | null;
  charts: CollectedChartLayerRef[];
  initialSnapshots?: ChartSnapshotItem[];
  widgets?: CollectedHtmlWidgetRef[];
  /** Process-local pending I-7 receipts awaiting settlement without resending to the platform. */
  pendingReceipts?: Map<string, PendingChartDeliveryReceipt>;
  waitTimeoutMs?: number;
  pollIntervalMs?: number;
  nowFn?: () => number;
  sleepFn?: (ms: number) => Promise<void>;
}

export interface DeliverChartsAndWidgetsResult {
  settled: boolean;
  /** True when at least one chart/widget was delivered in this call or an earlier settled delivery. */
  delivered?: boolean;
  /** True when another worker holds an active `in_flight` claim or an I-7 receipt is still pending settlement. */
  inFlight?: boolean;
  /** True when at least one outbound send ended with uncertain delivery outcome (`outcome: "unknown"`). */
  uncertain?: boolean;
  sentImages: number;
  sentLinks: number;
  timedOutPending: boolean;
}

/**
 * Deliver collected `chart_layer` items (PNG + web link in DM when ready; link-only in group or
 * on failure/timeout) and `html_widget` items (link-only) under deterministic key `charts:${runId}`.
 */
export async function deliverTurnChartsAndWidgets(
  params: DeliverChartsAndWidgetsParams,
): Promise<DeliverChartsAndWidgetsResult> {
  // Deduplicate charts by `(chart_id, layer_id)` while merging metadata from initialSnapshots
  const chartOrder: string[] = [];
  const chartByKey = new Map<string, CollectedChartLayerRef>();

  for (const c of params.charts) {
    const lid = c.layerId.trim();
    const cid = c.chartId.trim();
    if (!lid || !cid) continue;
    const ck = chartLayerCompositeKey(cid, lid);
    if (!chartByKey.has(ck)) {
      chartOrder.push(ck);
    }
    chartByKey.set(ck, { ...c, chartId: cid, layerId: lid });
  }
  for (const snap of params.initialSnapshots ?? []) {
    const lid = snap.layer_id.trim();
    const cid = snap.chart_id.trim();
    if (!lid || !cid) continue;
    const ck = chartLayerCompositeKey(cid, lid);
    if (!chartByKey.has(ck)) {
      chartOrder.push(ck);
      chartByKey.set(ck, {
        chartId: cid,
        layerId: lid,
        symbol: snap.symbol ?? null,
        timeframe: snap.timeframe ?? null,
        title: snap.title ?? null,
        workspaceId: snap.workspace_id ?? null,
      });
    }
  }

  const uniqueWidgets: CollectedHtmlWidgetRef[] = [];
  const seenWidgets = new Set<string>();
  for (const w of params.widgets ?? []) {
    const aid = w.artifactId.trim();
    if (!aid || seenWidgets.has(aid)) continue;
    seenWidgets.add(aid);
    uniqueWidgets.push({ ...w, artifactId: aid });
  }

  const orderedCharts = chartOrder
    .slice(0, MAX_CHARTS_PER_TURN)
    .map((ck) => chartByKey.get(ck)!);

  if (orderedCharts.length === 0 && uniqueWidgets.length === 0) {
    return {
      settled: true,
      delivered: false,
      sentImages: 0,
      sentLinks: 0,
      timedOutPending: false,
    };
  }

  const adapter = params.adapters.get(params.platform);
  if (!adapter) {
    return {
      settled: false,
      delivered: false,
      sentImages: 0,
      sentLinks: 0,
      timedOutPending: false,
    };
  }

  const deliveryKey = params.deliveryKey ?? `charts:${params.runId}`;
  const deliveryKind = params.deliveryKind ?? "fallback";
  const totalItems = orderedCharts.length + uniqueWidgets.length;
  const pendingReceipts =
    params.pendingReceipts ?? DEFAULT_PENDING_CHART_RECEIPTS;

  let claim: I6AuthorizeAndClaimResponse;
  try {
    claim = await params.backend.authorizeAndClaimDelivery({
      delivery_key: deliveryKey,
      kind: deliveryKind,
      user_id: params.userId,
      thread_id: params.threadId,
      run_id: params.runId,
      execution_id: params.executionId ?? null,
      platform: params.platform,
      target: params.targetAddress,
      inbound_message_id: params.inboundMessageId ?? null,
      has_non_empty_text: false,
      total_chunks: totalItems,
      source_scope_key:
        deliveryKind === "automation"
          ? null
          : params.sourceScopeKey ?? params.threadScopeKey ?? null,
      source_generation:
        deliveryKind === "automation" ? null : params.sourceGeneration ?? null,
      activity_origin:
        deliveryKind === "automation"
          ? "notification"
          : params.activityOrigin ?? "conversation",
    });
  } catch (err) {
    if (isStaleGatewayEpochError(err)) throw err;
    return {
      settled: false,
      delivered: false,
      inFlight: pendingReceipts.has(deliveryKey),
      sentImages: 0,
      sentLinks: 0,
      timedOutPending: false,
    };
  }

  if (claim.claim_status === "already_completed") {
    pendingReceipts.delete(deliveryKey);
    const completedStatus =
      claim.status ??
      (typeof claim.first_response_json?.status === "string"
        ? claim.first_response_json.status
        : undefined);
    const alreadyDelivered =
      completedStatus === undefined
        ? true
        : completedStatus === "sent" || completedStatus === "partial";
    return {
      settled: true,
      delivered: alreadyDelivered,
      inFlight: false,
      sentImages: 0,
      sentLinks: 0,
      timedOutPending: false,
    };
  }
  if (claim.claim_status !== "claimed") {
    const inFlight =
      claim.claim_status === "in_flight_or_unknown" &&
      claim.existing_status !== "unknown";
    if (inFlight) {
      const pending = pendingReceipts.get(deliveryKey);
      const attemptMatches =
        pending !== undefined &&
        (pending.deliveryAttempt === undefined ||
          claim.delivery_attempt === undefined ||
          pending.deliveryAttempt === claim.delivery_attempt);
      if (attemptMatches) {
        const retryAttempt =
          pending.deliveryAttempt ??
          (typeof claim.delivery_attempt === "number"
            ? claim.delivery_attempt
            : undefined);
        try {
          await params.backend.updateDeliveryProgress(
            {
              delivery_key: pending.deliveryKey,
              sent_chunks: pending.sentImages + pending.sentLinks,
              total_chunks: pending.totalItems,
              platform_message_ids: pending.platformMessageIds,
              status: pending.finalStatus,
              outcome: pending.finalStatus,
              ...(pending.finalStatus === "unknown"
                ? { error_code: "unavailable" }
                : {}),
              delivery_attempt: retryAttempt,
            },
            { platform: pending.platform },
          );
          pendingReceipts.delete(deliveryKey);
          const settled =
            pending.finalStatus === "sent" || pending.finalStatus === "partial";
          return {
            settled,
            delivered: settled && pending.sentImages + pending.sentLinks > 0,
            inFlight: false,
            ...(pending.finalStatus === "unknown" ? { uncertain: true } : {}),
            sentImages: pending.sentImages,
            sentLinks: pending.sentLinks,
            timedOutPending: pending.timedOutPending,
          };
        } catch (err) {
          if (isStaleGatewayEpochError(err)) throw err;
          if (err instanceof BackendHttpError && err.status === 409) {
            pendingReceipts.delete(deliveryKey);
          }
          return {
            settled: false,
            delivered: false,
            inFlight: true,
            sentImages: pending.sentImages,
            sentLinks: pending.sentLinks,
            timedOutPending: pending.timedOutPending,
          };
        }
      }
    } else {
      pendingReceipts.delete(deliveryKey);
    }
    return {
      settled: claim.claim_status === "denied",
      delivered: false,
      inFlight,
      sentImages: 0,
      sentLinks: 0,
      timedOutPending: false,
    };
  }

  pendingReceipts.delete(deliveryKey);

  const effectiveTarget = claim.target_address ?? params.targetAddress;
  const recipientId = claim.recipient_id ?? claim.dm_recipient_platform_user_id ?? null;
  const deliveryAttempt =
    typeof claim.delivery_attempt === "number" ? claim.delivery_attempt : undefined;
  const isDm = isDmTargetAddress(effectiveTarget);
  const externalTopicId = claim.external_topic_id ?? null;
  const replyToMessageId =
    !isDm && params.platform === "feishu"
      ? claim.inbound_message_id ?? claim.first_reply_message_id ?? claim.root_message_id ?? null
      : null;
  const replyInThread = !isDm && params.platform === "feishu" && Boolean(externalTopicId);

  // Wait up to 20s for DM pending snapshots (Postgres authoritative)
  const waited =
    params.outboundBlocked === true
      ? {
          snapshotsByKey: new Map<string, ChartSnapshotItem>(),
          snapshotsByLayerId: new Map<string, ChartSnapshotItem>(),
          outboundBlocked: true,
          statusConfirmed: true,
          timedOut: false,
        }
      : await waitForChartSnapshotsReady({
          backend: params.backend,
          runId: params.runId,
          threadId: params.threadId,
          platform: params.platform,
          initialSnapshots: params.initialSnapshots,
          expectedChartKeys: orderedCharts.map((c) =>
            chartLayerCompositeKey(c.chartId, c.layerId),
          ),
          outboundBlocked: params.outboundBlocked,
          isDm,
          timeoutMs: params.waitTimeoutMs,
          pollIntervalMs: params.pollIntervalMs,
          nowFn: params.nowFn,
          sleepFn: params.sleepFn,
        });

  const isOutboundBlocked = Boolean(params.outboundBlocked === true || waited.outboundBlocked);
  const isStatusUnconfirmed = isDm && !isOutboundBlocked && !waited.statusConfirmed;
  const useFixedChartCopy = isOutboundBlocked || isStatusUnconfirmed || !isDm;

  const sentMessageIds: string[] = [];
  let sentImages = 0;
  let sentLinks = 0;
  let failures = 0;
  let unknowns = 0;
  let chunkIndex = 0;

  const isUncertainDeliveryError = (err: unknown): boolean =>
    err instanceof ChannelDeliveryError && err.outcome === "unknown";

  const verifyDmRecipient = async (): Promise<void> => {
    if (
      params.platform === "weixin" ||
      typeof params.backend.verifyDmDeliveryRecipient !== "function"
    ) {
      return;
    }
    const res = await params.backend.verifyDmDeliveryRecipient(
      deliveryKey,
      deliveryAttempt,
    );
    if (!res.authorized) {
      throw new Error(res.deny_code || res.code || "dm_recipient_denied");
    }
  };

  const sendChartOrWidgetItem = async (opts: {
    title: string;
    chartId?: string;
    workspaceId?: string | null;
    webUrl: string | null;
    imageBuffer?: Buffer | null;
    imageFilename?: string;
    imageMimeType?: string;
    isWidget?: boolean;
    fixedCopy?: boolean;
  }): Promise<"sent" | "failed" | "unknown"> => {
    const idx = chunkIndex++;
    const effectiveFixed = Boolean(opts.fixedCopy || opts.isWidget);

    const sendAttempt = async (withImage: boolean): Promise<boolean> => {
      const imgBuf = withImage && !effectiveFixed ? (opts.imageBuffer ?? null) : null;

      // 1. Feishu: Schema 2.0 card with embedded PNG (`__img__`) for DM image, or link button card
      if (params.platform === "feishu" && typeof adapter.sendCard === "function") {
        const hasImage =
          Boolean(imgBuf) && imgBuf!.byteLength <= FEISHU_CHART_IMAGE_MAX_BYTES;
        const placeholder = hasImage ? `chart_snap_${idx}` : undefined;
        const card = buildFeishuChartSnapshotCard({
          title: opts.title,
          chartId: opts.chartId,
          imagePlaceholder: placeholder,
          webUrl: opts.webUrl,
          isWidget: opts.isWidget,
          fixedCopy: effectiveFixed,
        });
        const images: SendCardImageInput[] | undefined = hasImage
          ? [
              {
                placeholder: placeholder!,
                content: imgBuf!,
                mimeType: opts.imageMimeType ?? "image/png",
                filename: opts.imageFilename ?? "chart_snapshot.png",
              },
            ]
          : undefined;
        const res = await adapter.sendCard({
          targetAddress: effectiveTarget,
          recipientPlatformUserId: recipientId,
          card: card as unknown as Record<string, unknown>,
          images,
          replyToMessageId,
          replyInThread,
          webBaseUrl: params.webBaseUrl ?? null,
          threadId: params.threadId ?? null,
          workspaceId: opts.workspaceId ?? null,
          verifyRecipientBeforeSend: hasImage && isDm ? verifyDmRecipient : undefined,
        });
        if (res.messageId) sentMessageIds.push(res.messageId);
        if (res.usedMarkdownFallback) {
          if (opts.webUrl) {
            sentLinks += 1;
            return true;
          }
          // Fallback stripped the image/buttons and there was no verified chart link.
          return false;
        }
        if (hasImage) sentImages += 1;
        else sentLinks += 1;
        return true;
      }

      // 2. Discord: multipart PNG + ActionRow Link Button in DM; text + Link Button otherwise
      if (params.platform === "discord") {
        const buttonLabel = opts.isWidget ? "打开网页交互组件" : "打开网页交互版";
        const components = opts.webUrl
          ? buildDiscordLinkButtonRow(buttonLabel, opts.webUrl)
          : undefined;
        if (imgBuf && typeof adapter.sendTextWithFile === "function") {
          const caption = opts.webUrl
            ? `看盘图表：${opts.title}`
            : buildTextDeepLinkMessage({
                title: opts.title,
                webUrl: null,
                isWidget: opts.isWidget,
                fixedCopy: effectiveFixed,
              });
          const res = await adapter.sendTextWithFile({
            targetAddress: effectiveTarget,
            recipientPlatformUserId: recipientId,
            path: opts.imageFilename ?? "chart_snapshot.png",
            filename: opts.imageFilename ?? "chart_snapshot.png",
            content: imgBuf,
            mimeType: opts.imageMimeType ?? "image/png",
            text: caption,
            chunkIndex: idx,
            totalChunks: totalItems,
            threadScopeKey: params.threadScopeKey ?? null,
            deliveryKey,
            deliveryAttempt,
            verifyRecipientBeforeSend: isDm ? verifyDmRecipient : undefined,
            components,
          });
          if (res.messageId) sentMessageIds.push(res.messageId);
          sentImages += 1;
          return true;
        }
        const text = buildTextDeepLinkMessage({
          title: opts.title,
          webUrl: opts.webUrl,
          isWidget: opts.isWidget,
          fixedCopy: effectiveFixed,
        });
        const res = await adapter.sendTextChunk({
          targetAddress: effectiveTarget,
          recipientPlatformUserId: recipientId,
          text,
          chunkIndex: idx,
          totalChunks: totalItems,
          threadScopeKey: params.threadScopeKey ?? null,
          externalTopicId,
          confirmedThreadAddress: claim.confirmed_thread_address ?? null,
          deliveryKey,
          deliveryAttempt,
          webBaseUrl: params.webBaseUrl ?? null,
          threadId: params.threadId ?? null,
          components,
        });
        if (res.messageId) sentMessageIds.push(res.messageId);
        sentLinks += 1;
        return true;
      }

      // 3. Weixin (or generic adapter): encrypted IMAGE message + deep link text in DM
      const effectiveDeliveryAttempt = deliveryAttempt ?? 1;
      const weixinPreflight =
        params.platform === "weixin"
          ? await resolveWeixinPreflightForChart(
              params.backend,
              deliveryKey,
              effectiveDeliveryAttempt,
              idx,
            )
          : null;
      if (params.platform === "weixin" && !weixinPreflight) {
        return false;
      }

      if (imgBuf) {
        const res = await adapter.sendFile({
          targetAddress: effectiveTarget,
          recipientPlatformUserId: recipientId,
          path: opts.imageFilename ?? "chart_snapshot.png",
          filename: opts.imageFilename ?? "chart_snapshot.png",
          content: imgBuf,
          mimeType: opts.imageMimeType ?? "image/png",
          threadScopeKey: params.threadScopeKey ?? null,
          deliveryKey,
          deliveryAttempt,
          weixinPreflight,
          refreshWeixinPreflight:
            params.platform === "weixin"
              ? () =>
                  params.backend.preflightWeixinSend(deliveryKey, {
                    delivery_attempt: effectiveDeliveryAttempt,
                    chunk_index: idx,
                  })
              : undefined,
        });
        if (res.messageId) sentMessageIds.push(res.messageId);
        sentImages += 1;
        // Also send public web interactive deep link text when reachable (§2.3(2))
        if (opts.webUrl) {
          const linkText = `网页交互版：${opts.webUrl}`;
          const linkRes = await adapter.sendTextChunk({
            targetAddress: effectiveTarget,
            recipientPlatformUserId: recipientId,
            text: linkText,
            chunkIndex: idx,
            totalChunks: totalItems,
            threadScopeKey: params.threadScopeKey ?? null,
            deliveryKey,
            deliveryAttempt,
            weixinPreflight,
          });
          if (linkRes.messageId) sentMessageIds.push(linkRes.messageId);
        }
        return true;
      }

      const text = buildTextDeepLinkMessage({
        title: opts.title,
        webUrl: opts.webUrl,
        isWidget: opts.isWidget,
        fixedCopy: effectiveFixed,
      });
      const res = await adapter.sendTextChunk({
        targetAddress: effectiveTarget,
        recipientPlatformUserId: recipientId,
        text,
        chunkIndex: idx,
        totalChunks: totalItems,
        threadScopeKey: params.threadScopeKey ?? null,
        externalTopicId,
        deliveryKey,
        deliveryAttempt,
        weixinPreflight,
      });
      if (res.messageId) sentMessageIds.push(res.messageId);
      sentLinks += 1;
      return true;
    };

    const wantsImage = Boolean(opts.imageBuffer) && !effectiveFixed;
    if (wantsImage) {
      try {
        return (await sendAttempt(true)) ? "sent" : "failed";
      } catch (err) {
        if (isStaleGatewayEpochError(err)) throw err;
        if (isUncertainDeliveryError(err)) {
          return "unknown";
        }
        // Only confirmed undelivered errors degrade cleanly to link-only message
        // (matching the download-403 fallback path).
        try {
          return (await sendAttempt(false)) ? "sent" : "failed";
        } catch (fallbackErr) {
          if (isStaleGatewayEpochError(fallbackErr)) throw fallbackErr;
          if (isUncertainDeliveryError(fallbackErr)) {
            return "unknown";
          }
          return "failed";
        }
      }
    }

    try {
      return (await sendAttempt(false)) ? "sent" : "failed";
    } catch (err) {
      if (isStaleGatewayEpochError(err)) throw err;
      if (isUncertainDeliveryError(err)) {
        return "unknown";
      }
      return "failed";
    }
  };

  for (const chart of orderedCharts) {
    const title = useFixedChartCopy
      ? "看盘图表"
      : chart.title?.trim() || chart.chartId;
    const webUrl = buildChartLayerDeepLink(
      params.webBaseUrl,
      params.threadId,
      chart.chartId,
      chart.layerId,
    );
    const snap = waited.snapshotsByKey.get(
      chartLayerCompositeKey(chart.chartId, chart.layerId),
    );
    let pngBuffer: Buffer | null = null;
    let filename = `chart_${chart.layerId}.png`;
    let mimeType = "image/png";

    if (
      isDm &&
      !isOutboundBlocked &&
      !useFixedChartCopy &&
      snap &&
      snap.status === "ready" &&
      snap.snapshot_key &&
      typeof params.backend.downloadChartSnapshot === "function"
    ) {
      try {
        const downloaded = await params.backend.downloadChartSnapshot({
          snapshotKey: snap.snapshot_key,
          runId: params.runId,
          targetAddress: effectiveTarget,
          deliveryKey,
          deliveryAttempt,
          maxBytes:
            params.platform === "feishu"
              ? FEISHU_CHART_IMAGE_MAX_BYTES
              : undefined,
        });
        pngBuffer = downloaded.content;
        filename = downloaded.filename;
        mimeType = downloaded.mimeType;
      } catch (err) {
        if (isStaleGatewayEpochError(err)) throw err;
        // Any download/auth/size failure degrades cleanly to web interactive deep link.
        pngBuffer = null;
      }
    }

    const outcome = await sendChartOrWidgetItem({
      title,
      chartId: useFixedChartCopy ? undefined : chart.chartId,
      workspaceId: chart.workspaceId ?? null,
      webUrl,
      imageBuffer: pngBuffer,
      imageFilename: filename,
      imageMimeType: mimeType,
      isWidget: false,
      fixedCopy: useFixedChartCopy,
    });
    if (outcome === "unknown") unknowns += 1;
    else if (outcome === "failed") failures += 1;
  }

  for (const widget of uniqueWidgets) {
    const webUrl = buildHtmlWidgetDeepLink(
      params.webBaseUrl,
      params.threadId,
      widget.artifactId,
      widget.monitorSlug,
    );
    const outcome = await sendChartOrWidgetItem({
      title: "交互组件",
      webUrl,
      imageBuffer: null,
      isWidget: true,
      fixedCopy: true,
    });
    if (outcome === "unknown") unknowns += 1;
    else if (outcome === "failed") failures += 1;
  }

  const finalStatus: "sent" | "partial" | "failed" | "unknown" =
    unknowns > 0
      ? "unknown"
      : failures === 0
        ? "sent"
        : sentImages + sentLinks > 0
          ? "partial"
          : "failed";

  try {
    await params.backend.updateDeliveryProgress(
      {
        delivery_key: deliveryKey,
        sent_chunks: sentImages + sentLinks,
        total_chunks: totalItems,
        platform_message_ids: sentMessageIds,
        status: finalStatus,
        outcome: finalStatus,
        ...(finalStatus === "unknown" ? { error_code: "unavailable" } : {}),
        delivery_attempt: deliveryAttempt,
      },
      { platform: params.platform },
    );
  } catch (err) {
    if (isStaleGatewayEpochError(err)) throw err;
    rememberPendingChartReceipt(pendingReceipts, {
      deliveryKey,
      platform: params.platform,
      deliveryAttempt,
      sentImages,
      sentLinks,
      totalItems,
      platformMessageIds: sentMessageIds,
      finalStatus,
      timedOutPending: waited.timedOut,
    });
    return {
      settled: false,
      delivered: false,
      inFlight: true,
      ...(finalStatus === "unknown" ? { uncertain: true } : {}),
      sentImages,
      sentLinks,
      timedOutPending: waited.timedOut,
    };
  }

  const settled = finalStatus === "sent" || finalStatus === "partial";
  return {
    settled,
    delivered: settled && sentImages + sentLinks > 0,
    inFlight: false,
    ...(finalStatus === "unknown" ? { uncertain: true } : {}),
    sentImages,
    sentLinks,
    timedOutPending: waited.timedOut,
  };
}
