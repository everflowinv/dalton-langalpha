import type { ReadyAttachment } from '@/components/ui/chat-input.types';
import { selectionPriceBounds } from './selectionBounds';
import type { WidgetContextSnapshot } from '@/pages/Dashboard/widgets/framework/contextSnapshot';
import type { ChartSelection } from '@/pages/MarketView/stores/chartSelectionStore';

/** An image by its MIME type, the server's `image` tag, or its name, for a file the browser could not type. */
export function isImageFile({ type = '', name = '' }: { type?: string; name?: string }): boolean {
  return type.startsWith('image/') || type === 'image' || /\.(jpe?g|png|gif|webp)$/i.test(name);
}

export interface Attachment {
  file: File;
  dataUrl?: string | null;
  type: string;
  attachmentId?: string;
  // DALTON-HOOK(chat-large-upload): server-issued upload reference for stored attachments
  uploadId?: string;
}

export type AttachmentContext =
  | {
      type: 'image' | 'pdf' | 'file';
      attachment_id: string;
      description: string;
    }
  | {
      type: 'chat_upload';
      upload_id: string;
      name: string;
      mime_type: string;
      size: number;
    }
  | {
      type: string;
      data: string;
      description: string;
    };

/**
 * The sent bubble's record of an attachment, until history replaces it with
 * the server's. A staged file carries itself, which survives the route state
 * a send from another page travels in; a chart capture carries its image as
 * `dataUrl`, having no file behind it.
 */
export type AttachmentMeta = {
  name?: string;
  filename?: string;
  type: string;
  mime_type?: string;
  size?: number;
  file?: File;
  dataUrl?: string | null;
  preview?: string | null;
  url?: string;
  // DALTON-HOOK(chat-large-upload): preserve server upload id on client draft/bubble metadata
  upload_id?: string;
  uploadId?: string;
  attachment_id?: string;
  /** History only: the turn never received this file, so there is nothing to show. */
  missing?: boolean;
  /** When the staged file became ready, kept so a send handed back to the
   *  composer re-stages it before the server's expiry. Never sent. */
  readyAt?: number;
};

/**
 * What a send carries for its attached files: `additional_context` items
 * (`chat_upload` for Dalton chunked uploads, `attachment_id` for upstream
 * staged files) and the bubble's record of them.
 */
export function attachmentSendParts(files: readonly ReadyAttachment[]): { contexts: AttachmentContext[]; meta: AttachmentMeta[] } {
  const contexts: AttachmentContext[] = [];
  const meta: AttachmentMeta[] = [];
  for (const a of files) {
    if (a.uploadId) {
      contexts.push({
        type: 'chat_upload',
        upload_id: a.uploadId,
        name: a.file.name,
        mime_type: a.type || 'application/octet-stream',
        size: a.file.size,
      });
      meta.push({
        name: a.file.name,
        type: a.type,
        size: a.file.size,
        file: a.file,
        upload_id: a.uploadId,
      });
    } else if (a.attachmentId) {
      contexts.push({
        type: isImageFile({ type: a.type, name: a.file.name }) ? 'image' : a.type === 'application/pdf' ? 'pdf' : 'file',
        attachment_id: a.attachmentId,
        description: a.file.name,
      });
      meta.push({
        name: a.file.name,
        type: a.type,
        size: a.file.size,
        file: a.file,
        ...(a.readyAt !== undefined ? { readyAt: a.readyAt } : {}),
      });
    } else if (a.dataUrl != null) {
      contexts.push({
        type: isImageFile({ type: a.type, name: a.file.name }) ? 'image' : a.type === 'application/pdf' ? 'pdf' : 'file',
        data: a.dataUrl,
        description: a.file.name,
      });
      meta.push({
        name: a.file.name,
        type: a.type,
        size: a.file.size,
        file: a.file,
        dataUrl: a.dataUrl,
      });
    }
  }
  return { contexts, meta };
}

/** Whether a send's `additional_context` names files staged on the server. */
export function namesStagedFiles(contexts: readonly Record<string, unknown>[] | null | undefined): boolean {
  return !!contexts?.some((c) => typeof c.attachment_id === 'string');
}

/**
 * Convert attachments to additional_context format with accurate type tags.
 * Stored uploads → "chat_upload" reference (no inline bytes);
 * staged files → "attachment_id" reference;
 * inline images → "image", PDFs → "pdf", everything else → "file".
 */
// DALTON-HOOK(chat-large-upload): serialize stored uploads by upload_id without base64 payloads
export function attachmentsToContexts(attachments: Attachment[]): AttachmentContext[] {
  const result: AttachmentContext[] = [];
  for (const a of attachments) {
    if (a.uploadId) {
      result.push({
        type: 'chat_upload',
        upload_id: a.uploadId,
        name: a.file.name,
        mime_type: a.type || 'application/octet-stream',
        size: a.file.size,
      });
      continue;
    }
    if (a.attachmentId) {
      result.push({
        type: isImageFile({ type: a.type, name: a.file.name })
          ? 'image'
          : a.type === 'application/pdf'
            ? 'pdf'
            : 'file',
        attachment_id: a.attachmentId,
        description: a.file.name,
      });
      continue;
    }
    if (a.dataUrl != null) {
      result.push({
        type: a.type.startsWith('image/')
          ? 'image'
          : a.type === 'application/pdf'
            ? 'pdf'
            : 'file',
        data: a.dataUrl,
        description: a.file.name,
      });
    }
  }
  return result;
}

/**
 * The staged files a send carries, as the composer holds them, read back from
 * what `attachmentSendParts` made of them: its items name them in the order
 * its records hold the files.
 */
// DALTON-HOOK(chat-large-upload): round-trip both staged attachment_id and chat_upload upload_id through draft restore
export function stagedFilesOf(
  contexts: readonly Record<string, unknown>[] | null | undefined,
  meta: readonly AttachmentMeta[] | null | undefined,
): ReadyAttachment[] {
  const files = (meta ?? []).filter((m): m is AttachmentMeta & { file: File } => !!m.file);
  const refs = (contexts ?? []).flatMap((c): Array<{ attachmentId: string; uploadId?: string }> => {
    if (typeof c.attachment_id === 'string') return [{ attachmentId: c.attachment_id }];
    if (c.type === 'chat_upload' && typeof c.upload_id === 'string') return [{ attachmentId: '', uploadId: c.upload_id }];
    return [];
  });
  return refs.slice(0, files.length).map((ref, i) => ({
    file: files[i].file,
    type: files[i].type,
    attachmentId: ref.attachmentId,
    ...(ref.uploadId ? { uploadId: ref.uploadId } : {}),
    ...(files[i].readyAt !== undefined ? { readyAt: files[i].readyAt } : {}),
  }));
}

/**
 * Per-widget snapshot serialization for `additional_context`.
 *
 * Each snapshot becomes ONE `{type:"widget", ...}` item. Image-bearing
 * snapshots additionally produce a sibling `{type:"image", ...}` item that
 * rides the existing MultimodalContext channel — the backend modality gate
 * handles vision-vs-text-only routing without further code changes.
 *
 * Pre-flight size guard: a structured-clone error from `navigate(state)` will
 * crash the dashboard → chat handoff, so we cap the structured `data` payload
 * at ~100KB per item. Oversized `data` is dropped (the rendered `text` and
 * the optional image still ride along — those are what the agent reads).
 */
const MAX_DATA_BYTES_PER_SNAPSHOT = 100 * 1024;

type WidgetCtxItem = {
  type: 'widget';
  widget_type: string;
  widget_id: string;
  label: string;
  text: string;
  data: Record<string, unknown>;
  captured_at?: string;
  description?: string;
};

type WidgetImageItem = {
  type: 'image';
  data: string;
  description: string;
};

export function widgetSnapshotsToContexts(
  snapshots: WidgetContextSnapshot[],
): Array<WidgetCtxItem | WidgetImageItem> {
  const out: Array<WidgetCtxItem | WidgetImageItem> = [];
  for (const s of snapshots) {
    let data = s.data ?? {};
    try {
      const sz = new Blob([JSON.stringify(data)]).size;
      if (sz > MAX_DATA_BYTES_PER_SNAPSHOT) {
        // Drop the structured payload; the rendered text + image still ship.
        data = { _truncated: true, _original_bytes: sz };
      }
    } catch {
      data = { _truncated: true };
    }
    out.push({
      type: 'widget',
      widget_type: s.widget_type,
      widget_id: s.widget_id,
      label: s.label,
      text: s.text,
      data,
      captured_at: s.captured_at,
      description: s.description,
    });
    if (s.image_jpeg_data_url) {
      out.push({
        type: 'image',
        data: s.image_jpeg_data_url,
        description: s.label,
      });
    }
  }
  return out;
}

type ChartSelectionCtxItem = {
  type: 'chart_selection';
  symbol: string;
  timeframe: string;
  selection_type: 'region' | 'price_level';
  time_start?: string;
  time_end?: string;
  price_low: number;
  price_high: number;
  bars: ChartSelection['bars'];
  bars_truncated: boolean;
  label?: string;
};

type ChartSelectionImageItem = {
  type: 'image';
  data: string;
  description: string;
};

/**
 * A selection time as the agent's structured block prints it: the exchange's
 * wall clock with no zone. The chart stamps that wall clock as UTC, so the `Z`
 * on the ISO string is false and a model that honored it would shift the time.
 */
function chartTimeText(iso: string, timeframe: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const text = d.toISOString();
  return timeframe === '1day' ? text.slice(0, 10) : `${text.slice(0, 10)} ${text.slice(11, 16)}`;
}

/** Caption for a region's cropped screenshot — the structured item holds the exact values. */
export function describeSelectionImage(sel: ChartSelection): string {
  const range = selectionPriceBounds(sel, 'en-US');
  const span =
    sel.timeStart && sel.timeEnd
      ? `, ${chartTimeText(sel.timeStart, sel.timeframe)} → ${chartTimeText(sel.timeEnd, sel.timeframe)}`
      : '';
  return `Cropped chart image of ${sel.symbol} ${sel.timeframe} (price ${range}${span})`;
}

/**
 * Map a user's chart selection to `additional_context` items.
 *
 * Emits one structured `chart_selection` item (bounds + per-candle OHLCV the
 * agent can analyze and draw back onto the exact region). A region selection
 * with a cropped screenshot also emits a sibling `{type:"image", ...}` item
 * that rides the existing MultimodalContext channel — a vision-capable model
 * sees the region directly, others fall back to the bars via the backend
 * modality gate. The structured bars stay the primary signal.
 *
 * Pass `liveSymbol`/`liveTimeframe` to drop a stale selection (the chart was
 * switched to a different instance after the user drew it).
 */
export function chartSelectionToContext(
  sel: ChartSelection,
  live?: { symbol?: string | null; timeframe?: string | null },
): Array<ChartSelectionCtxItem | ChartSelectionImageItem> {
  if (live) {
    const liveSym = (live.symbol ?? '').toUpperCase();
    const liveTf = live.timeframe ?? '';
    if ((liveSym && liveSym !== sel.symbol) || (liveTf && liveTf !== sel.timeframe)) {
      return [];
    }
  }

  const comment = sel.comment?.trim();
  const item: ChartSelectionCtxItem = {
    type: 'chart_selection',
    symbol: sel.symbol,
    timeframe: sel.timeframe,
    selection_type: sel.selectionType,
    price_low: sel.priceLow,
    price_high: sel.priceHigh,
    bars: sel.bars,
    bars_truncated: sel.barsTruncated,
  };
  if (sel.selectionType === 'region') {
    item.time_start = sel.timeStart;
    item.time_end = sel.timeEnd;
  }
  // The user's per-selection note rides as `label`, separate from the message.
  if (comment) item.label = comment;

  const out: Array<ChartSelectionCtxItem | ChartSelectionImageItem> = [item];
  if (sel.croppedImage) {
    out.push({ type: 'image', data: sel.croppedImage, description: describeSelectionImage(sel) });
  }
  return out;
}
