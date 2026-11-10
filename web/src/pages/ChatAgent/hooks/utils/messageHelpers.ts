/**
 * Message creation and manipulation utilities
 * Provides helper functions for creating and updating message objects
 */

import type {
  ChatMessage,
  AssistantMessage,
  UserMessage,
  NotificationMessage,
  NotificationVariant,
  WakeupArrival,
} from '@/types/chat';
// DALTON-HOOK(chat-large-upload): Attachment type for normalized user and steered attachments
import type { Attachment } from '@/types/sse';
import type { ArtifactCommentsShown } from '@/types/api';
import type { WidgetContextSnapshot } from '@/pages/Dashboard/widgets/framework/contextSnapshot';
import type { ChartSelectionSnapshot } from '@/pages/MarketView/stores/chartSelectionStore';

// Re-export types for consumers
export type { ChatMessage, AssistantMessage, UserMessage, NotificationMessage, NotificationVariant };

// Keeps ids made within the same millisecond apart: a message is updated by
// id, so two sharing one are both updated.
let _idSeq = 0;

export interface AttachmentMeta {
  file?: File;
  name?: string;
  filename?: string;
  dataUrl?: string | null;
  preview?: string | null;
  url?: string;
  type?: string;
  mime_type?: string;
  size?: number;
  upload_id?: string;
  uploadId?: string;
}

// DALTON-HOOK(chat-large-upload): normalize caller-owned and server-returned attachment metadata into Attachment[]
function readUploadId(item: Record<string, unknown> | undefined): string | undefined {
  if (!item) return undefined;
  if (typeof item.upload_id === 'string' && item.upload_id) return item.upload_id;
  if (typeof item.uploadId === 'string' && item.uploadId) return item.uploadId;
  return undefined;
}

function normalizeAttachmentRecord(raw: unknown, prior?: Attachment): Attachment | null {
  if (!raw || typeof raw !== 'object') return null;
  const rec = raw as Record<string, unknown>;
  const priorRec = prior as Record<string, unknown> | undefined;
  const fileObj = rec.file instanceof File
    ? rec.file
    : priorRec?.file instanceof File
      ? (priorRec.file as File)
      : undefined;
  const name = (typeof rec.name === 'string' && rec.name)
    ? rec.name
    : (typeof rec.filename === 'string' && rec.filename)
      ? rec.filename
      : fileObj?.name ?? prior?.name ?? '';
  if (!name) return null;

  const mimeType = (typeof rec.mime_type === 'string' && rec.mime_type)
    ? rec.mime_type
    : (typeof rec.mimeType === 'string' && rec.mimeType)
      ? rec.mimeType
      : (typeof priorRec?.mime_type === 'string' && priorRec.mime_type)
        ? priorRec.mime_type
        : fileObj?.type || undefined;
  const type = (typeof rec.type === 'string' && rec.type)
    ? rec.type
    : mimeType ?? prior?.type ?? 'application/octet-stream';
  const size = typeof rec.size === 'number'
    ? rec.size
    : typeof rec.size_bytes === 'number'
      ? rec.size_bytes
      : typeof prior?.size === 'number'
        ? prior.size
        : fileObj?.size;
  const uploadId = readUploadId(rec) ?? readUploadId(priorRec);
  const status = (typeof rec.status === 'string' && rec.status)
    ? rec.status
    : (typeof priorRec?.status === 'string' && priorRec.status)
      ? priorRec.status
      : undefined;
  const url = (typeof rec.url === 'string' && rec.url)
    ? rec.url
    : (typeof prior?.url === 'string' && prior.url)
      ? prior.url
      : undefined;
  const dataUrl = (typeof rec.dataUrl === 'string' && rec.dataUrl)
    ? rec.dataUrl
    : (typeof priorRec?.dataUrl === 'string' && priorRec.dataUrl)
      ? priorRec.dataUrl
      : undefined;
  const preview = (typeof rec.preview === 'string' && rec.preview)
    ? rec.preview
    : (typeof priorRec?.preview === 'string' && priorRec.preview)
      ? priorRec.preview
      : undefined;

  const normalized: Attachment = {
    name,
    type,
    ...(size !== undefined ? { size } : {}),
    ...(url ? { url } : {}),
  };
  if (mimeType) normalized.mime_type = mimeType;
  if (uploadId) normalized.upload_id = uploadId;
  if (status) normalized.status = status;
  if (dataUrl) normalized.dataUrl = dataUrl;
  if (preview) normalized.preview = preview;
  if (fileObj) normalized.file = fileObj;
  return normalized;
}

export function normalizeAttachments(raw: unknown): Attachment[] | undefined {
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const items: Attachment[] = [];
  for (const entry of raw) {
    const normalized = normalizeAttachmentRecord(entry);
    if (normalized) items.push(normalized);
  }
  return items.length > 0 ? items : undefined;
}

export function mergeAcceptedAttachments(
  existing: readonly Attachment[] | undefined,
  incoming: unknown,
): Attachment[] | undefined {
  if (!Array.isArray(incoming) || incoming.length === 0) {
    return existing && existing.length > 0 ? [...existing] : undefined;
  }
  const pool = existing ? [...existing] : [];
  const usedIndices = new Set<number>();
  const merged: Attachment[] = [];

  for (const entry of incoming) {
    if (!entry || typeof entry !== 'object') continue;
    const rec = entry as Record<string, unknown>;
    const incomingUid = readUploadId(rec);
    const incomingName = typeof rec.name === 'string' && rec.name
      ? rec.name
      : typeof rec.filename === 'string' && rec.filename
        ? rec.filename
        : '';
    let matchIdx = -1;
    if (incomingUid) {
      matchIdx = pool.findIndex(
        (p, idx) => !usedIndices.has(idx) && readUploadId(p as Record<string, unknown>) === incomingUid,
      );
    }
    if (matchIdx === -1 && incomingName) {
      matchIdx = pool.findIndex((p, idx) => !usedIndices.has(idx) && p.name === incomingName);
    }
    const prior = matchIdx >= 0 ? pool[matchIdx] : undefined;
    if (matchIdx >= 0) usedIndices.add(matchIdx);
    const normalized = normalizeAttachmentRecord(entry, prior);
    if (normalized) merged.push(normalized);
  }

  // Keep caller-owned inline attachments (no upload_id) that rode alongside stored uploads.
  pool.forEach((p, idx) => {
    if (usedIndices.has(idx)) return;
    if (!readUploadId(p as Record<string, unknown>)) merged.push(p);
  });

  return merged.length > 0 ? merged : undefined;
}

/**
 * `turnIndex` is the backend turn the bubble belongs to, or undefined for one
 * whose turn the transcript's count names (see projectTurns). Required, so
 * every bubble the view creates decides its turn where it is made. `id` is a
 * bubble already on screen that the message takes over.
 */
export function createUserMessage(
  message: string,
  turnIndex: number | undefined,
  { attachments, widgetSnapshots, chartSelections, artifactComments, id }: {
    // DALTON-HOOK(chat-large-upload): normalize caller-owned and server attachment shapes into Attachment[]
    attachments?: ReadonlyArray<AttachmentMeta | Attachment | Record<string, unknown>> | null;
    widgetSnapshots?: WidgetContextSnapshot[] | null;
    chartSelections?: ChartSelectionSnapshot[] | null;
    artifactComments?: ArtifactCommentsShown | null;
    id?: string;
  } = {},
): UserMessage {
  const msg: UserMessage = {
    id: id ?? `user-${Date.now()}-${_idSeq++}`,
    role: 'user',
    content: message,
    contentType: 'text',
    timestamp: new Date(),
    isStreaming: false,
    turnIndex,
    // Every caller is a send from this view; history builds its own rows.
    sentHere: true,
  };
  const normalizedAttachments = normalizeAttachments(attachments);
  if (normalizedAttachments) {
    msg.attachments = normalizedAttachments;
  }
  if (widgetSnapshots && widgetSnapshots.length > 0) {
    msg.widgetSnapshots = widgetSnapshots;
  }
  if (chartSelections && chartSelections.length > 0) {
    msg.chartSelections = chartSelections;
  }
  if (artifactComments && artifactComments.comments.length > 0) {
    msg.artifactComments = artifactComments;
  }
  return msg;
}

/** Whether a user message carried more than its text: an attachment, a chart
 *  selection, a widget or file comments, which a send of its text alone
 *  would drop. */
export function carriesContext(message: UserMessage): boolean {
  return !!(message.attachments?.length || message.chartSelections?.length || message.widgetSnapshots?.length
    || message.artifactComments?.comments.length);
}

/** `turnIndex` as for createUserMessage. */
export function createAssistantMessage(id: string, turnIndex: number | undefined): AssistantMessage {
  return {
    id,
    role: 'assistant',
    content: '',
    contentType: 'text',
    timestamp: new Date(),
    isStreaming: true,
    contentSegments: [],
    reasoningProcesses: {},
    toolCallProcesses: {},
    todoListProcesses: {},
    turnIndex,
  };
}

export function updateMessage<T extends { id: string }>(
  messages: T[],
  messageId: string,
  updater: (msg: T) => T,
): T[] {
  return messages.map((msg) => {
    if (msg.id !== messageId) return msg;
    return updater(msg);
  });
}

export function insertMessage<T extends { id: string }>(
  messages: T[],
  insertIndex: number,
  newMessage: T,
): T[] {
  return [
    ...messages.slice(0, insertIndex),
    newMessage,
    ...messages.slice(insertIndex),
  ];
}

export function appendMessage<T extends { id: string }>(messages: T[], newMessage: T): T[] {
  return [...messages, newMessage];
}

/**
 * Creates a notification message for inline dividers (e.g. compaction, offload).
 * ``detail`` is the optional expandable text (e.g. the compaction summary).
 */
export function createNotificationMessage(
  text: string,
  variant: NotificationVariant = 'info',
  detail?: string,
): NotificationMessage {
  return {
    id: `notification-${Date.now()}-${_idSeq++}`,
    role: 'notification',
    content: text,
    variant,
    timestamp: new Date(),
    detail,
  };
}

/**
 * The row a wakeup, or a price alert, draws in the transcript. The id is the
 * caller's, so the same notice keeps one id across a live stream and every
 * replay.
 */
export function createWakeupMessage(
  id: string,
  wakeup: WakeupArrival,
  timestamp: Date,
): NotificationMessage {
  return {
    id,
    role: 'notification',
    content: '',
    variant: 'info',
    timestamp,
    wakeup,
  };
}
