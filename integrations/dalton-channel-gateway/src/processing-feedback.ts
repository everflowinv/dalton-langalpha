import type {
  ChannelAdapter,
  ProcessingFeedbackHandle,
  ProcessingFeedbackOutcome,
} from "./channels/adapter.js";
import type { StatusFeedbackConfig } from "./config.js";
import { createMemoryDedupe, type MemoryDedupeGuard } from "./shared/dedupe.js";
import {
  createSubsystemLogger,
  type SubsystemLogger,
} from "./shared/logger.js";
import type { ChannelPlatform, InboundChannelEvent } from "./types.js";

export type ProcessingFeedbackRunRole = "primary" | "steered";

export const DEFAULT_STATUS_FEEDBACK_CONFIG: StatusFeedbackConfig = {
  enabled: true,
  platforms: {
    feishu: true,
    discord: true,
    weixin: true,
  },
};

export function normalizeStatusFeedbackConfig(
  config?: Partial<StatusFeedbackConfig> & {
    platforms?: Partial<StatusFeedbackConfig["platforms"]>;
  },
): StatusFeedbackConfig {
  return {
    enabled: config?.enabled ?? true,
    platforms: {
      feishu: config?.platforms?.feishu ?? true,
      discord: config?.platforms?.discord ?? true,
      weixin: config?.platforms?.weixin ?? true,
    },
  };
}

interface ManagedFeedbackEntry {
  readonly msgKey: string;
  readonly platform: ChannelPlatform;
  readonly messageId: string;
  readonly primaryRunIds: Set<string>;
  readonly steeredRunIds: Set<string>;
  handle: ProcessingFeedbackHandle | null;
  initDone: boolean;
  settled: boolean;
  pendingOutcome: ProcessingFeedbackOutcome | null;
}

const MAX_REMEMBERED_DELIVERED_RUNS = 2_000;
const DELIVERED_RUN_TTL_MS = 10 * 60 * 1_000;

/**
 * Coordinates best-effort processing status feedback across inbound turns,
 * steered follow-ups, interrupts, and first-reply deliveries.
 *
 * State is stored strictly in gateway process memory; reactions or indicators
 * left behind before a gateway restart are intentionally not cleaned up on boot.
 */
export class ProcessingFeedbackCoordinator {
  private readonly adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  private readonly config: StatusFeedbackConfig;
  private readonly logger: SubsystemLogger;
  private readonly activeByMessage = new Map<string, ManagedFeedbackEntry>();
  private readonly activePrimaryByRun = new Map<
    string,
    Set<ManagedFeedbackEntry>
  >();
  private readonly activeSteeredByRun = new Map<
    string,
    Set<ManagedFeedbackEntry>
  >();
  private readonly deliveredRuns = new Map<
    string,
    { outcome: ProcessingFeedbackOutcome; atMs: number }
  >();
  private readonly startedMessageKeys: MemoryDedupeGuard;

  constructor(options: {
    adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
    config?: StatusFeedbackConfig;
    logger?: SubsystemLogger;
  }) {
    this.adapters = options.adapters;
    this.config = normalizeStatusFeedbackConfig(options.config);
    this.logger =
      options.logger ?? createSubsystemLogger("gateway/status-feedback");
    this.startedMessageKeys = createMemoryDedupe({
      maxSize: 5_000,
      ttlMs: 30 * 60 * 1_000,
    });
  }

  isEnabled(platform: ChannelPlatform): boolean {
    return this.config.enabled && this.config.platforms[platform];
  }

  /**
   * Starts processing feedback for an inbound message without blocking the caller
   * on platform API I/O. If an active handle already exists for `(platform, messageId)`
   * (e.g. in-process recovery via `recoverDetachedInboundTurn`), it is reused cleanly.
   */
  begin(event: InboundChannelEvent): void {
    if (!this.isEnabled(event.platform)) {
      return;
    }
    const messageId = event.messageId.trim();
    if (!messageId) {
      return;
    }
    const msgKey = `${event.platform}:${messageId}`;
    if (
      this.activeByMessage.has(msgKey) ||
      this.startedMessageKeys.peek(msgKey)
    ) {
      return;
    }

    const adapter = this.adapters.get(event.platform);
    if (!adapter?.beginProcessingFeedback) {
      return;
    }

    this.startedMessageKeys.record(msgKey);

    const entry: ManagedFeedbackEntry = {
      msgKey,
      platform: event.platform,
      messageId,
      primaryRunIds: new Set<string>(),
      steeredRunIds: new Set<string>(),
      handle: null,
      initDone: false,
      settled: false,
      pendingOutcome: null,
    };
    this.activeByMessage.set(msgKey, entry);

    let result:
      | ProcessingFeedbackHandle
      | null
      | Promise<ProcessingFeedbackHandle | null>;
    try {
      result = adapter.beginProcessingFeedback({ event });
    } catch (err) {
      entry.initDone = true;
      entry.settled = true;
      this.detach(entry);
      this.logger.warn("Processing feedback begin threw synchronously", {
        platform: event.platform,
        messageId,
        error: err,
      });
      return;
    }

    if (
      result &&
      typeof (result as Promise<ProcessingFeedbackHandle | null>).then ===
        "function"
    ) {
      void (result as Promise<ProcessingFeedbackHandle | null>).then(
        (resolvedHandle) => {
          entry.initDone = true;
          entry.handle = resolvedHandle;
          if (!resolvedHandle) {
            entry.settled = true;
            this.detach(entry);
            return;
          }
          if (entry.settled && entry.pendingOutcome) {
            this.invokeHandleSettle(entry, resolvedHandle, entry.pendingOutcome);
          }
        },
        (err) => {
          entry.initDone = true;
          entry.settled = true;
          this.detach(entry);
          this.logger.warn("Processing feedback begin rejected", {
            platform: event.platform,
            messageId,
            error: err,
          });
        },
      );
      return;
    }

    const syncHandle = result as ProcessingFeedbackHandle | null;
    entry.initDone = true;
    entry.handle = syncHandle;
    if (!syncHandle) {
      entry.settled = true;
      this.detach(entry);
    }
  }

  /**
   * Ensures a steered follow-up that was NOT consumed by the host run and is
   * resubmitted as a brand-new turn has active processing feedback for the new turn.
   * If the handle is still active, detaches it from old steered run bindings and
   * reuses it; if the host run already cleared it, re-starts feedback cleanly.
   */
  reopenForResubmit(event: InboundChannelEvent): void {
    const messageId = event.messageId.trim();
    if (!messageId) {
      return;
    }
    const msgKey = `${event.platform}:${messageId}`;
    const existing = this.activeByMessage.get(msgKey);
    if (existing && !existing.settled) {
      this.detachRunBindings(existing);
      return;
    }
    this.startedMessageKeys.delete(msgKey);
    this.begin(event);
  }

  /**
   * Associates an active inbound feedback entry with a backend run ID.
   * - `"primary"`: the message that started this run. If an early reply for
   *   `runId` already arrived before `metadata`/`turn_start`, settles immediately.
   * - `"steered"`: a follow-up merged into an already-running turn. Kept alive
   *   across interim `/agent/send` messages until the host run finishes or interrupts.
   */
  bindRun(
    target: Pick<InboundChannelEvent, "platform" | "messageId">,
    runId: string | null | undefined,
    role: ProcessingFeedbackRunRole = "primary",
  ): void {
    const trimmedRunId = runId?.trim();
    if (!trimmedRunId) {
      return;
    }
    const msgKey = `${target.platform}:${target.messageId.trim()}`;
    const entry = this.activeByMessage.get(msgKey);
    if (!entry || entry.settled) {
      return;
    }

    if (role === "primary") {
      const earlyDelivery = this.getRememberedDeliveredRun(trimmedRunId);
      if (earlyDelivery) {
        this.settleEntry(entry, earlyDelivery.outcome);
        return;
      }
      entry.primaryRunIds.add(trimmedRunId);
      let set = this.activePrimaryByRun.get(trimmedRunId);
      if (!set) {
        set = new Set<ManagedFeedbackEntry>();
        this.activePrimaryByRun.set(trimmedRunId, set);
      }
      set.add(entry);
      return;
    }

    entry.steeredRunIds.add(trimmedRunId);
    let set = this.activeSteeredByRun.get(trimmedRunId);
    if (!set) {
      set = new Set<ManagedFeedbackEntry>();
      this.activeSteeredByRun.set(trimmedRunId, set);
    }
    set.add(entry);
  }

  settleMessage(
    target: Pick<InboundChannelEvent, "platform" | "messageId">,
    outcome: ProcessingFeedbackOutcome,
  ): void {
    const msgKey = `${target.platform}:${target.messageId.trim()}`;
    const entry = this.activeByMessage.get(msgKey);
    if (!entry) {
      return;
    }
    this.settleEntry(entry, outcome);
  }

  /** Settles only the primary message(s) that initiated `runId`, leaving steered follow-ups active. */
  settleRunPrimary(
    runId: string | null | undefined,
    outcome: ProcessingFeedbackOutcome,
  ): void {
    const trimmedRunId = runId?.trim();
    if (!trimmedRunId) {
      return;
    }
    const primaries = this.activePrimaryByRun.get(trimmedRunId);
    if (!primaries || primaries.size === 0) {
      return;
    }
    for (const entry of [...primaries]) {
      this.settleEntry(entry, outcome);
    }
  }

  /** Settles all entries (both primary and steered follow-ups) bound to `runId`. */
  settleRun(
    runId: string | null | undefined,
    outcome: ProcessingFeedbackOutcome,
  ): void {
    const trimmedRunId = runId?.trim();
    if (!trimmedRunId) {
      return;
    }
    const primaries = this.activePrimaryByRun.get(trimmedRunId);
    if (primaries && primaries.size > 0) {
      for (const entry of [...primaries]) {
        this.settleEntry(entry, outcome);
      }
    }
    const steered = this.activeSteeredByRun.get(trimmedRunId);
    if (steered && steered.size > 0) {
      for (const entry of [...steered]) {
        this.settleEntry(entry, outcome);
      }
    }
  }

  /**
   * Called by `DeliveryService` as soon as the first reply chunk/file or interrupt
   * prompt lands in the current conversation.
   * - `agent_send`: settles only the primary message for `runId` (and records the
   *   delivered run so a late `metadata`/`turn_start` `bindRun` settles immediately);
   *   steered follow-ups remain active until the host turn finishes.
   * - `fallback` / `interrupt`: settles both primary and steered entries on `runId`.
   */
  onFirstCurrentDelivery(info: {
    platform: ChannelPlatform;
    runId?: string | null;
    inboundMessageId?: string | null;
    kind: "agent_send" | "fallback" | "interrupt";
  }): void {
    const outcome: ProcessingFeedbackOutcome =
      info.kind === "interrupt" ? "interrupted" : "done";
    if (info.runId?.trim()) {
      this.rememberDeliveredRun(info.runId.trim(), outcome);
    }
    if (info.inboundMessageId) {
      this.settleMessage(
        { platform: info.platform, messageId: info.inboundMessageId },
        outcome,
      );
    }
    if (info.runId) {
      if (info.kind === "agent_send") {
        this.settleRunPrimary(info.runId, outcome);
      } else {
        this.settleRun(info.runId, outcome);
      }
    }
  }

  settleAll(outcome: ProcessingFeedbackOutcome = "failed"): void {
    for (const entry of [...this.activeByMessage.values()]) {
      this.settleEntry(entry, outcome);
    }
  }

  private rememberDeliveredRun(
    runId: string,
    outcome: ProcessingFeedbackOutcome,
  ): void {
    const now = Date.now();
    if (
      this.deliveredRuns.size >= MAX_REMEMBERED_DELIVERED_RUNS &&
      !this.deliveredRuns.has(runId)
    ) {
      const oldestKey = this.deliveredRuns.keys().next().value;
      if (oldestKey !== undefined) {
        this.deliveredRuns.delete(oldestKey);
      }
    }
    this.deliveredRuns.set(runId, { outcome, atMs: now });
  }

  private getRememberedDeliveredRun(
    runId: string,
  ): { outcome: ProcessingFeedbackOutcome; atMs: number } | undefined {
    const found = this.deliveredRuns.get(runId);
    if (!found) {
      return undefined;
    }
    if (Date.now() - found.atMs > DELIVERED_RUN_TTL_MS) {
      this.deliveredRuns.delete(runId);
      return undefined;
    }
    return found;
  }

  private settleEntry(
    entry: ManagedFeedbackEntry,
    outcome: ProcessingFeedbackOutcome,
  ): void {
    if (entry.settled) {
      return;
    }
    entry.settled = true;
    entry.pendingOutcome = outcome;
    this.detach(entry);
    if (entry.handle) {
      this.invokeHandleSettle(entry, entry.handle, outcome);
    }
  }

  private detachRunBindings(entry: ManagedFeedbackEntry): void {
    for (const runId of entry.primaryRunIds) {
      const set = this.activePrimaryByRun.get(runId);
      if (set) {
        set.delete(entry);
        if (set.size === 0) {
          this.activePrimaryByRun.delete(runId);
        }
      }
    }
    entry.primaryRunIds.clear();

    for (const runId of entry.steeredRunIds) {
      const set = this.activeSteeredByRun.get(runId);
      if (set) {
        set.delete(entry);
        if (set.size === 0) {
          this.activeSteeredByRun.delete(runId);
        }
      }
    }
    entry.steeredRunIds.clear();
  }

  private detach(entry: ManagedFeedbackEntry): void {
    if (this.activeByMessage.get(entry.msgKey) === entry) {
      this.activeByMessage.delete(entry.msgKey);
    }
    this.detachRunBindings(entry);
  }

  private invokeHandleSettle(
    entry: ManagedFeedbackEntry,
    handle: ProcessingFeedbackHandle,
    outcome: ProcessingFeedbackOutcome,
  ): void {
    try {
      const res = handle.settle(outcome);
      if (res && typeof (res as Promise<void>).then === "function") {
        void (res as Promise<void>).catch((err) => {
          this.logger.warn("Processing feedback settle rejected", {
            platform: entry.platform,
            messageId: entry.messageId,
            outcome,
            error: err,
          });
        });
      }
    } catch (err) {
      this.logger.warn("Processing feedback settle threw synchronously", {
        platform: entry.platform,
        messageId: entry.messageId,
        outcome,
        error: err,
      });
    }
  }
}
