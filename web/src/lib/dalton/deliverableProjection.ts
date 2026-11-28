/**
 * Dalton unified phase/tool-boundary/continuation-aware deliverable text selection
 * shared by both collapsed view (`contentProjection.ts`) and copy/preview (`messageText.ts`).
 *
 * Kept strictly pure (no React/DOM imports) so node-environment tests importing
 * `messageText.ts` never pull in browser globals.
 */
import type { ContentSegmentRecord, ToolCallProcessRecord } from '@/pages/ChatAgent/components/messageList/types';
import { inChunkOrder } from '@/pages/ChatAgent/session/stream/textChunks';

export type DeliverableTextFollow = 'nothing' | 'work' | 'ask';

const NON_BLANK = /\S/;
const BOOKKEEPING_TOOLS = new Set(['UpdatePlan', 'TodoWrite']);
const ASKS_USER = new Set([
  'user_question',
  'tool_approval',
  'create_workspace',
  'start_question',
  'ptc_agent',
  'delete_workspace',
  'stop_workspace',
  'delete_thread',
  'archive_workspace', // DALTON-HOOK(cos-settings-tools)
  'credit_pause',
]);

function textBlockKey(order: number): string {
  return `text-${order}`;
}

function groupDeliverableSegments(segments: readonly ContentSegmentRecord[]): ContentSegmentRecord[] {
  const groups: ContentSegmentRecord[] = [];
  let run: ContentSegmentRecord[] = [];
  const closeRun = () => {
    if (!run.length) return;
    const first = run[0];
    const continuedFrom = run.find((s) => s.continuedFromMessageId)?.continuedFromMessageId;
    groups.push({
      type: 'text',
      content: run.length === 1 ? first.content : run.map((s) => s.content || '').join(''),
      order: first.order,
      ...(first.phase ? { phase: first.phase } : {}),
      ...(first.messageId ? { messageId: first.messageId } : {}),
      ...(continuedFrom ? { continuedFromMessageId: continuedFrom } : {}),
    });
    run = [];
  };
  for (const segment of inChunkOrder(segments)) {
    if (segment.type === 'text') {
      const prev = run[0];
      if (prev && (prev.phase !== segment.phase || prev.messageId !== segment.messageId)) {
        closeRun();
      }
      run.push(segment);
    } else {
      closeRun();
      groups.push(segment);
    }
  }
  closeRun();
  return groups;
}

export interface DeliverableTextBlockLike {
  type: string;
  key: string;
  segment?: ContentSegmentRecord;
}

/**
 * Single source of truth for what followed each non-commentary text block
 * (nothing, business work, or an interactive ask card).
 *
 * Excludes ``phase === 'commentary'`` FIRST so a business tool call after a
 * commentary segment still marks the preceding non-commentary preamble as
 * ``'work'`` (preventing pre-tool preamble from surviving into collapsed view
 * or copy).
 */
export function readDeliverableTextFollows(
  groups: readonly ContentSegmentRecord[],
  toolCallProcesses: Record<string, ToolCallProcessRecord | undefined>,
): Map<string, DeliverableTextFollow> {
  const follows = new Map<string, DeliverableTextFollow>();
  let key: string | null = null;
  for (const group of groups) {
    if (group.type === 'text') {
      if (group.phase === 'commentary') continue;
      if (!group.content || !NON_BLANK.test(group.content)) continue;
      key = textBlockKey(group.order);
      follows.set(key, 'nothing');
    } else if (key === null || group.type === 'reasoning' || group.type === 'notification') {
      continue;
    } else if (ASKS_USER.has(group.type)) {
      follows.set(key, 'ask');
    } else if (follows.get(key) === 'nothing') {
      if (group.type === 'tool_call' && !group.toolCallId) {
        continue;
      }
      const toolName =
        group.type === 'tool_call' && group.toolCallId
          ? toolCallProcesses[group.toolCallId]?.toolName
          : undefined;
      if (toolName === undefined || !BOOKKEEPING_TOOLS.has(toolName)) {
        follows.set(key, 'work');
      }
    }
  }
  return follows;
}

/**
 * Single shared selector used by BOTH collapsed rendering and copy/preview:
 * 1. Excludes ``phase === 'commentary'`` and blank text;
 * 2. Drops pre-tool preamble before the last business tool boundary (except
 *    text leading directly into an interactive ``'ask'`` card);
 * 3. Keeps the closing reply and its ``continuedFromMessageId`` chain after
 *    the last business tool boundary (plus any qualifying un-IDed post-tool
 *    blocks).
 */
export function selectDeliverableTextKeys(params: {
  entries: readonly { key: string; segment?: ContentSegmentRecord }[];
  textsFollowed: ReadonlyMap<string, DeliverableTextFollow>;
  closingKey?: string;
  useExplicitClosingKey?: boolean;
  scoring?: {
    lastTextKey: string | undefined;
    endsReply: boolean;
    answerScore: (text: string) => number;
    heldThreshold: number;
    shownThreshold: number;
  };
}): Set<string> {
  const { entries, textsFollowed, scoring } = params;
  const shown = new Set<string>();

  const deliverableEntries = entries.filter(
    (entry) =>
      entry.segment?.phase !== 'commentary' &&
      textsFollowed.has(entry.key),
  );

  let lastWorkIdx = -1;
  for (let i = 0; i < deliverableEntries.length; i++) {
    if (textsFollowed.get(deliverableEntries[i].key) === 'work') {
      lastWorkIdx = i;
    }
  }

  const postToolEntries = deliverableEntries.slice(lastWorkIdx + 1);
  const effectiveClosingKey = params.useExplicitClosingKey
    ? params.closingKey
    : postToolEntries.length > 0
      ? postToolEntries[postToolEntries.length - 1].key
      : undefined;

  const chainKeys = new Set<string>();
  let closingHasMsgId = false;
  if (effectiveClosingKey) {
    const closingIdx = postToolEntries.findIndex((b) => b.key === effectiveClosingKey);
    if (closingIdx >= 0) {
      chainKeys.add(effectiveClosingKey);
      const neededIds = new Set<string>();
      const closingSeg = postToolEntries[closingIdx].segment;
      if (closingSeg?.messageId) {
        closingHasMsgId = true;
        neededIds.add(closingSeg.messageId);
      }
      if (closingSeg?.continuedFromMessageId) {
        neededIds.add(closingSeg.continuedFromMessageId);
      }
      for (let i = closingIdx - 1; i >= 0; i--) {
        const seg = postToolEntries[i].segment;
        if (seg?.messageId && neededIds.has(seg.messageId)) {
          chainKeys.add(postToolEntries[i].key);
          if (seg.continuedFromMessageId) {
            neededIds.add(seg.continuedFromMessageId);
          }
        }
      }
    }
  }

  for (let i = 0; i < deliverableEntries.length; i++) {
    const entry = deliverableEntries[i];
    const follow = textsFollowed.get(entry.key);
    if (follow === undefined) continue;
    if (follow === 'ask') {
      shown.add(entry.key);
      continue;
    }
    // Pre-tool text (before the last business tool boundary) never promotes into deliverable text.
    if (i <= lastWorkIdx) continue;
    if (entry.key === effectiveClosingKey || chainKeys.has(entry.key)) {
      shown.add(entry.key);
      continue;
    }
    // Distinct non-chained messages after the tool boundary do not merge into the final answer.
    if (closingHasMsgId && entry.segment?.messageId) continue;
    if (scoring) {
      const threshold =
        scoring.endsReply && entry.key === scoring.lastTextKey
          ? scoring.heldThreshold
          : scoring.shownThreshold;
      if (scoring.answerScore(entry.segment?.content ?? '') >= threshold) {
        shown.add(entry.key);
      }
    } else if (!entry.segment?.messageId) {
      shown.add(entry.key);
    }
  }

  return shown;
}

export function selectCollapsedAnswerTextKeys(params: {
  blocks: readonly DeliverableTextBlockLike[];
  roles: ReadonlyMap<string, string>;
  textsFollowed: ReadonlyMap<string, DeliverableTextFollow>;
  closingKey: string | undefined;
  lastTextKey: string | undefined;
  endsReply: boolean;
  answerScore: (text: string) => number;
  heldThreshold: number;
  shownThreshold: number;
}): Set<string> {
  const entries = params.blocks.filter(
    (block) => block.type === 'text' && params.roles.get(block.key) === 'text',
  );
  return selectDeliverableTextKeys({
    entries,
    textsFollowed: params.textsFollowed,
    closingKey: params.closingKey,
    useExplicitClosingKey: true,
    scoring: {
      lastTextKey: params.lastTextKey,
      endsReply: params.endsReply,
      answerScore: params.answerScore,
      heldThreshold: params.heldThreshold,
      shownThreshold: params.shownThreshold,
    },
  });
}

/**
 * Select the assistant's deliverable copy text via the exact same
 * ``readDeliverableTextFollows`` + ``selectDeliverableTextKeys`` pipeline used
 * by collapsed rendering.
 */
export function selectDeliverableAssistantText(
  message: { contentSegments?: unknown; content?: unknown; toolCallProcesses?: unknown },
  max = Infinity,
): string {
  const rawSegments = message.contentSegments as ContentSegmentRecord[] | undefined;
  if (!rawSegments || rawSegments.length === 0) {
    return typeof message.content === 'string' ? message.content.slice(0, max) : '';
  }

  const toolCalls = (message.toolCallProcesses ?? {}) as Record<string, ToolCallProcessRecord | undefined>;
  const groups = groupDeliverableSegments(rawSegments);
  const sawAnyText = groups.some((g) => g.type === 'text');
  if (!sawAnyText) {
    return typeof message.content === 'string' ? message.content.slice(0, max) : '';
  }

  const textsFollowed = readDeliverableTextFollows(groups, toolCalls);
  const entries = groups
    .filter((g) => g.type === 'text')
    .map((g) => ({ key: textBlockKey(g.order), segment: g }));

  const keepKeys = selectDeliverableTextKeys({
    entries,
    textsFollowed,
  });

  let out = '';
  for (const entry of entries) {
    if (!keepKeys.has(entry.key)) continue;
    if (out.length >= max) break;
    out += (entry.segment.content ?? '').slice(0, max - out.length);
  }
  return out;
}
