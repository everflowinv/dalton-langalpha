export type LiveState = 'active' | 'completing' | 'completed' | 'failed';

export type ToolCallData = {
  args?: Record<string, unknown>;
};

export type ToolCallResultData = {
  content?: unknown;
  artifact?: Record<string, unknown>;
};

interface ActivityIdentity {
  id: string;
  _liveState: LiveState;
}

export interface ReasoningActivityItem extends ActivityIdentity {
  type: 'reasoning';
  content: string;
  reasoningStartedAt?: number;
  reasoningElapsedMs?: number;
}

export interface ToolActivityItem extends ActivityIdentity {
  type: 'tool_call';
  toolCallId: string;
  toolName: string;
  toolCall?: ToolCallData;
  toolCallResult?: ToolCallResultData;
  isComplete?: boolean;
  isFailed?: boolean;
  _recentlyCompleted?: boolean;
  _annotationStep?: boolean;
  /** The computer's root, which the row's title, pill and icon read its
   *  paths under. Stamped by the projection that built the row, so every
   *  reader of the row agrees with the filter that kept it. */
  root?: string | null;
}

export type ActivityItem = ReasoningActivityItem | ToolActivityItem;

export interface PreparingToolCallData {
  toolName?: string;
  argsLength: number;
}

export function isRunning(item: ActivityItem): boolean {
  return item.type === 'reasoning'
    ? item._liveState === 'active'
    : item._liveState === 'active' && !item.isComplete && !item._recentlyCompleted;
}
