/**
 * Per-key sequential task queue (`createSequentialQueue`).
 *
 * Adapted from `.agy/ref_openclaw_channels/feishu/dist/.setup/monitor.account-UbN1RhlP.mjs`
 * (lines 858–872, MIT, © OpenClaw contributors) for Design §4.1 & §4.2 (`src/shared/keyed-queue.ts`).
 *
 * Serializes inbound task execution per conversation key (Feishu `chat_id`,
 * Discord `channel_id`, Weixin `from_user_id`) while allowing independent keys
 * to run concurrently.
 */

import { raceWithTimeoutAndAbort } from "./async.js";

export type QueueTaskStatus = "completed" | "timed_out" | "failed";

export interface SequentialQueueOptions {
  taskTimeoutMs?: number;
  onTimeout?: (key: string, timeoutMs: number) => void;
  onError?: (key: string, error: unknown) => void;
}

export interface SequentialQueue {
  enqueue(key: string, task: () => Promise<void>): Promise<QueueTaskStatus>;
  hasPending(key: string): boolean;
  activeKeyCount(): number;
}

export function createSequentialQueue(
  options: SequentialQueueOptions = {},
): SequentialQueue {
  const queues = new Map<string, Promise<unknown>>();
  const { taskTimeoutMs, onTimeout, onError } = options;

  const enqueue = (
    key: string,
    task: () => Promise<void>,
  ): Promise<QueueTaskStatus> => {
    const normalizedKey = key.trim() || "__default__";
    const prev = queues.get(normalizedKey) ?? Promise.resolve();

    const executeTask = async (): Promise<QueueTaskStatus> => {
      try {
        if (
          typeof taskTimeoutMs === "number" &&
          Number.isFinite(taskTimeoutMs) &&
          taskTimeoutMs > 0
        ) {
          const raced = await raceWithTimeoutAndAbort(task(), {
            timeoutMs: taskTimeoutMs,
          });
          if (raced.status === "timeout") {
            onTimeout?.(normalizedKey, taskTimeoutMs);
            return "timed_out";
          }
          if (raced.status === "rejected") {
            onError?.(normalizedKey, raced.error);
            throw raced.error;
          }
          return "completed";
        }
        await task();
        return "completed";
      } catch (err) {
        onError?.(normalizedKey, err);
        throw err;
      }
    };

    const next = prev.then(executeTask, executeTask);
    const tracked = next.then(
      () => undefined,
      () => undefined,
    );
    queues.set(normalizedKey, tracked);

    const cleanup = (): void => {
      if (queues.get(normalizedKey) === tracked) {
        queues.delete(normalizedKey);
      }
    };
    tracked.then(cleanup, cleanup);
    return next;
  };

  return {
    enqueue,
    hasPending: (key: string) => queues.has(key.trim() || "__default__"),
    activeKeyCount: () => queues.size,
  };
}
