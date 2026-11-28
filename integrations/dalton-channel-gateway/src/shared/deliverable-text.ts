/**
 * Unified deliverable-text accumulator and empty-delivery notice helpers (§5).
 *
 * Mirrors ``src/dalton/channels/deliverable_text.py`` so live SSE streaming,
 * disconnected ``/messages/replay``, thread output, and automation excerpts
 * extract byte-identical core deliverable text.
 */

import { createHash } from "node:crypto";

export const CHANNEL_EMPTY_AFTER_TOOLS_NOTICE =
  "已完成工具执行，但未生成最终文字总结；可回复“继续”获取结论，或在工作区查看执行结果。";

export const CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE =
  "已完成工具执行，但向当前会话发送结果未成功且未生成文字总结；可回复“继续”重发结论，或在工作区查看执行结果。";

export const AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY =
  "已完成工具步骤执行，未输出文字摘要。";

const OPENING_HOLD_LIMIT = 2000;
const NEED_PHRASE = "need more thinking";
const OPEN_TAG_RE = /<\s*(thinking_mode|thinking)\s*>/i;
const CLOSE_TAG_RES: Record<string, RegExp> = {
  thinking_mode: /<\s*\/\s*thinking_mode\s*>/gi,
  thinking: /<\s*\/\s*thinking\s*>/gi,
};
const BLANK_LINE_RE = /\n[ \t\r]*\n/;

const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp"]);
const IMAGE_MD_RE = /!\[([^\]]*)\]\(([^)]+)\)/g;
const IMAGE_REF_RE = /(!?)\[([^\]]*)\]\(([^)]+)\)/g;

export interface DeliverableExtractionResult {
  text: string;
  sawBusinessToolCall: boolean;
}

export function isMainAgentLane(agent: unknown): boolean {
  if (agent === undefined || agent === null || agent === "") {
    return true;
  }
  if (typeof agent !== "string") {
    return false;
  }
  const lowered = agent.trim().toLowerCase();
  return !lowered.startsWith("task:") && !lowered.startsWith("subagent:");
}

export function isSandboxImagePath(path: string): boolean {
  if (
    path.startsWith("http://") ||
    path.startsWith("https://") ||
    path.startsWith("//") ||
    path.startsWith("data:")
  ) {
    return false;
  }
  const slashIdx = path.lastIndexOf("/");
  const basename = slashIdx >= 0 ? path.slice(slashIdx + 1) : path;
  const dotIdx = basename.lastIndexOf(".");
  if (dotIdx < 0) return false;
  const ext = basename.slice(dotIdx + 1).toLowerCase();
  return IMAGE_EXTS.has(ext);
}

/**
 * Resolve captured sandbox image paths in markdown embeds/links, matching
 * ``ptc_agent.agent.middleware.image_capture.resolve_image_paths``.
 */
export function resolveImagePaths(
  text: string,
  urlMap: Record<string, string>,
): string {
  if (!text || Object.keys(urlMap).length === 0) {
    return text;
  }
  IMAGE_REF_RE.lastIndex = 0;
  return text.replace(IMAGE_REF_RE, (full, bang: string, label: string, path: string) => {
    const url = urlMap[path];
    return url ? `${bang}[${label}](${url})` : full;
  });
}

export function extractUnresolvedSandboxImagePaths(text: string): string[] {
  if (!text) return [];
  const seen = new Set<string>();
  const paths: string[] = [];
  IMAGE_MD_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = IMAGE_MD_RE.exec(text)) !== null) {
    const p = match[2];
    if (p && isSandboxImagePath(p) && !seen.has(p)) {
      seen.add(p);
      paths.push(p);
    }
  }
  return paths;
}

export function containsUnresolvedSandboxImagePath(text: string): boolean {
  return extractUnresolvedSandboxImagePaths(text).length > 0;
}

export function mergeImagesByMessage(
  ...sources: Array<Readonly<Record<string, Readonly<Record<string, string>>>> | null | undefined>
): Record<string, Record<string, string>> {
  const merged: Record<string, Record<string, string>> = {};
  for (const src of sources) {
    if (!src || typeof src !== "object" || Array.isArray(src)) continue;
    for (const [msgId, mapping] of Object.entries(src)) {
      if (!msgId || !mapping || typeof mapping !== "object" || Array.isArray(mapping)) {
        continue;
      }
      const bucket = merged[msgId] ?? (merged[msgId] = {});
      ingestImageMap(bucket, mapping);
    }
  }
  return merged;
}

/**
 * Resolve the sandbox-image URL map scoped strictly to one ``agent_send`` delivery's
 * own capture / source message, never applying a later overwrite of the same sandbox
 * path from global ``run_end.images`` / ``replay_facts.images``.
 */
export function resolveDeliveryImageUrls(params: {
  deliveryKey?: string | null;
  runId?: string | null;
  sourceMessageId?: string | null;
  capturedImages?: Readonly<Record<string, string>> | null;
  imagesByMessage?: Readonly<Record<string, Readonly<Record<string, string>>>> | null;
  toolCallMessageIds?: Readonly<Record<string, string>> | null;
  globalImages?: Readonly<Record<string, string>> | null;
}): Record<string, string> {
  const ownCaptured: Record<string, string> = {};
  ingestImageMap(ownCaptured, params.capturedImages);

  let effectiveMsgId =
    typeof params.sourceMessageId === "string" && params.sourceMessageId.trim().length > 0
      ? params.sourceMessageId.trim()
      : null;

  if (!effectiveMsgId && params.deliveryKey && params.toolCallMessageIds) {
    const dk = params.deliveryKey.trim();
    const runPrefix = params.runId ? `${params.runId}:` : null;
    const tcId =
      runPrefix && dk.startsWith(runPrefix)
        ? dk.slice(runPrefix.length)
        : dk.includes(":")
          ? dk.slice(dk.lastIndexOf(":") + 1)
          : dk;
    const mapped =
      params.toolCallMessageIds[tcId] ?? params.toolCallMessageIds[dk] ?? null;
    if (typeof mapped === "string" && mapped.length > 0) {
      effectiveMsgId = mapped;
    }
  }

  const perMsgMap: Record<string, string> = {};
  if (effectiveMsgId && params.imagesByMessage) {
    ingestImageMap(perMsgMap, params.imagesByMessage[effectiveMsgId]);
  }

  if (Object.keys(ownCaptured).length > 0 || Object.keys(perMsgMap).length > 0) {
    return {
      ...perMsgMap,
      ...ownCaptured,
    };
  }

  // No delivery-time or message-scoped capture recorded: fall back to global images
  // ONLY for paths not already captured by another message in this turn (so a later
  // message's v2 overwrite of the same path never rewrites an earlier delivery).
  const globalMap: Record<string, string> = {};
  ingestImageMap(globalMap, params.globalImages);
  if (Object.keys(globalMap).length === 0) {
    return {};
  }
  const ownedByOtherMessage = new Set<string>();
  if (params.imagesByMessage) {
    for (const [mid, mapping] of Object.entries(params.imagesByMessage)) {
      if (effectiveMsgId && mid === effectiveMsgId) continue;
      if (!mapping || typeof mapping !== "object" || Array.isArray(mapping)) continue;
      for (const p of Object.keys(mapping)) {
        ownedByOtherMessage.add(p);
      }
    }
  }
  if (ownedByOtherMessage.size === 0) {
    return globalMap;
  }
  const safeGlobal: Record<string, string> = {};
  for (const [p, url] of Object.entries(globalMap)) {
    if (!ownedByOtherMessage.has(p)) {
      safeGlobal[p] = url;
    }
  }
  return safeGlobal;
}

/**
 * Generate a corrected delivery message strictly from the original delivered text
 * and its recorded unresolved sandbox image path(s), never substituting unrelated
 * post-tool summary text (e.g. "已完成"). Returns ``null`` when no recorded
 * unresolved path has a resolved URL in ``resolvedImageUrls``.
 */
export function buildImageCorrectionText(params: {
  originalText: string | null | undefined;
  unresolvedImagePaths?: ReadonlyArray<string> | null;
  resolvedImageUrls?: Readonly<Record<string, string>> | null;
}): string | null {
  const orig = typeof params.originalText === "string" ? params.originalText : "";
  if (orig.trim().length === 0) return null;
  const urlMap = params.resolvedImageUrls;
  if (!urlMap || Object.keys(urlMap).length === 0) return null;
  const expectedPaths =
    params.unresolvedImagePaths && params.unresolvedImagePaths.length > 0
      ? params.unresolvedImagePaths
      : extractUnresolvedSandboxImagePaths(orig);
  if (expectedPaths.length === 0) return null;
  const anyResolved = expectedPaths.some(
    (p) => typeof urlMap[p] === "string" && urlMap[p]!.length > 0,
  );
  if (!anyResolved) return null;
  const rewritten = resolveImagePaths(orig, urlMap);
  if (rewritten === orig) return null;
  return rewritten;
}

/**
 * Strip opening harness thinking markers (``<thinking_mode>...</thinking_mode>``,
 * ``<thinking>...</thinking>``, or ``NEED MORE THINKING`` opening paragraph),
 * matching ``src/server/utils/thinking_leak.py:visible_assistant_text(..., followed_by_tool_call=False)``.
 */
export function stripOpeningThinkingLeak(text: string): string {
  if (!text) return "";
  const windowStr = text.slice(0, OPENING_HOLD_LIMIT);
  let start = 0;
  while (start < windowStr.length && /\s/.test(windowStr[start]!)) {
    start++;
  }
  if (start >= windowStr.length) {
    return text;
  }

  const lowerSlice = windowStr.slice(start).toLowerCase();
  if (lowerSlice.startsWith(NEED_PHRASE)) {
    const rest = text.slice(start);
    const blankMatch = BLANK_LINE_RE.exec(rest);
    if (!blankMatch) {
      return "";
    }
    return text.slice(start + blankMatch.index + blankMatch[0].length);
  }

  const tail = windowStr.slice(start);
  const openMatch = OPEN_TAG_RE.exec(tail);
  if (!openMatch || openMatch.index !== 0) {
    return text;
  }
  const tagName = openMatch[1]!.toLowerCase();
  const afterOpen = start + openMatch[0].length;
  const closer = CLOSE_TAG_RES[tagName];
  if (!closer) {
    return text;
  }
  closer.lastIndex = afterOpen;
  const closeMatch = closer.exec(windowStr);
  if (!closeMatch) {
    return text;
  }
  return text.slice(closeMatch.index + closeMatch[0].length);
}

function extractDiscardedIds(payload: Record<string, unknown>): Set<string> {
  const ids = new Set<string>();
  for (const field of ["id", "stream_id"] as const) {
    const val = payload[field];
    if (typeof val === "string" && val.length > 0) {
      ids.add(val);
    }
  }
  const rawList = payload.ids;
  if (Array.isArray(rawList)) {
    for (const item of rawList) {
      if (typeof item === "string" && item.length > 0) {
        ids.add(item);
      }
    }
  }
  return ids;
}

function ingestImageMap(target: Record<string, string>, raw: unknown): void {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return;
  }
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (k.length > 0 && typeof v === "string" && v.length > 0) {
      target[k] = v;
    }
  }
}

function ingestImagesByMessage(
  target: Record<string, Record<string, string>>,
  raw: unknown,
): void {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return;
  }
  for (const [msgId, mapping] of Object.entries(raw as Record<string, unknown>)) {
    if (!msgId || !mapping || typeof mapping !== "object" || Array.isArray(mapping)) {
      continue;
    }
    const bucket = target[msgId] ?? (target[msgId] = {});
    ingestImageMap(bucket, mapping);
  }
}

const UUID_HEX_RE = /^[0-9a-f]{32}$/i;
const UUID_DASH_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function normalizeWorkspaceId(val: unknown): string {
  if (typeof val !== "string") return "";
  const s = val.trim();
  if (!s) return "";
  const lower = s.toLowerCase();
  if (UUID_DASH_RE.test(lower)) return lower;
  if (UUID_HEX_RE.test(lower)) {
    return `${lower.slice(0, 8)}-${lower.slice(8, 12)}-${lower.slice(12, 16)}-${lower.slice(16, 20)}-${lower.slice(20)}`;
  }
  return lower;
}

function hasValidToolCalls(rawCalls: unknown): boolean {
  if (!Array.isArray(rawCalls)) return false;
  return rawCalls.some(
    (tc) =>
      tc !== null &&
      typeof tc === "object" &&
      typeof (tc as Record<string, unknown>).name === "string" &&
      ((tc as Record<string, unknown>).name as string).trim().length > 0,
  );
}

/**
 * Compute canonical SHA-256 digest of a ``send_message`` payload's text and attachments,
 * identifying each attachment by its resolved ``(workspace_id, path)`` pair so same-named
 * files from different workspaces never collide. Matches
 * ``src.dalton.llm.near_empty_reply.compute_send_payload_digest``.
 */
export function computeSendPayloadDigest(
  text: string | null | undefined,
  files?: ReadonlyArray<
    | string
    | { path?: string | null; file_path?: string | null; workspace_id?: string | null }
  > | null,
  defaultWorkspaceId?: string | null,
): string {
  const normText = typeof text === "string" ? text.trim() : "";
  const fallbackWs = normalizeWorkspaceId(defaultWorkspaceId);
  const normFiles: Array<{ path: string; workspace_id: string }> = [];
  if (Array.isArray(files)) {
    for (const item of files) {
      if (typeof item === "string") {
        const s = item.trim();
        if (s.length > 0) {
          normFiles.push({ path: s, workspace_id: fallbackWs });
        }
      } else if (item && typeof item === "object") {
        const raw = item.path ?? item.file_path ?? "";
        if (typeof raw === "string" && raw.trim().length > 0) {
          const itemWs = normalizeWorkspaceId(item.workspace_id) || fallbackWs;
          normFiles.push({ path: raw.trim(), workspace_id: itemWs });
        }
      }
    }
  }
  normFiles.sort((a, b) =>
    a.workspace_id < b.workspace_id
      ? -1
      : a.workspace_id > b.workspace_id
        ? 1
        : a.path < b.path
          ? -1
          : a.path > b.path
            ? 1
            : 0,
  );
  // Match Python json.dumps({"files": ..., "text": ...}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
  const canonical = JSON.stringify({ files: normFiles, text: normText });
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

interface TextChainSegment {
  msgId: string | null;
  text: string;
}

export class DeliverableTextAccumulator {
  private segments: TextChainSegment[] = [];
  private chainIds = new Set<string>();
  private activeMsgId: string | null = null;
  private discardedIds = new Set<string>();
  private imageUrlMap: Record<string, string> = {};
  private imagesByMessage: Record<string, Record<string, string>> = {};
  private closeOutToolCallIds = new Set<string>();
  private pendingToolCallIds = new Set<string>();
  private pendingToolCallsByMsg = new Map<string, Set<string>>();
  private hasPendingUnnamedToolCall = false;
  private toolCallToMsgId: Record<string, string> = {};
  // Legacy (pre-strict) fallback tracking when DALTON_STRICT_DELIVERABLE_TEXT=0.
  private legacyCurrentSegmentText = "";
  private legacyLastCompletedSegmentText = "";
  public sawBusinessToolCall = false;

  private resetActive(): void {
    this.segments = [];
    this.chainIds.clear();
    this.activeMsgId = null;
  }

  private dropPendingToolCall(tcId: string | null): void {
    if (tcId) {
      this.pendingToolCallIds.delete(tcId);
      for (const msgTcs of this.pendingToolCallsByMsg.values()) {
        msgTcs.delete(tcId);
      }
    } else {
      this.hasPendingUnnamedToolCall = false;
    }
  }

  private confirmBusinessToolExecution(tcId: string | null = null): void {
    this.dropPendingToolCall(tcId);
    if (tcId === null) {
      this.hasPendingUnnamedToolCall = false;
    }
    if (this.legacyCurrentSegmentText.trim().length > 0) {
      this.legacyLastCompletedSegmentText = this.legacyCurrentSegmentText;
      this.legacyCurrentSegmentText = "";
    }
    this.sawBusinessToolCall = true;
    this.resetActive();
  }

  public setImageUrlMap(images: Record<string, string> | null | undefined): void {
    ingestImageMap(this.imageUrlMap, images);
  }

  public setImagesByMessage(
    imagesByMessage: Record<string, Record<string, string>> | null | undefined,
  ): void {
    ingestImagesByMessage(this.imagesByMessage, imagesByMessage);
  }

  public getGlobalImageUrlMap(): Record<string, string> {
    return { ...this.imageUrlMap };
  }

  public getImagesByMessage(): Record<string, Record<string, string>> {
    const out: Record<string, Record<string, string>> = {};
    for (const [msgId, perMsg] of Object.entries(this.imagesByMessage)) {
      out[msgId] = { ...perMsg };
    }
    return out;
  }

  public getToolCallMessageIds(): Record<string, string> {
    return { ...this.toolCallToMsgId };
  }

  public getResolvedImageUrlMap(): Record<string, string> {
    return this.getGlobalImageUrlMap();
  }

  public pushEvent(
    eventType: string,
    payload: Record<string, unknown> | null | undefined,
  ): void {
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (eventType === "run_end") {
      ingestImageMap(this.imageUrlMap, payload.images);
      ingestImagesByMessage(this.imagesByMessage, payload.images_by_message);
      return;
    }
    if (!isMainAgentLane(payload.agent)) {
      return;
    }

    if (eventType === "tool_calls") {
      const rawCalls = payload.tool_calls;
      const msgId =
        typeof payload.id === "string" && payload.id.length > 0
          ? payload.id
          : null;
      if (payload.close_out === true) {
        if (Array.isArray(rawCalls)) {
          for (const tc of rawCalls) {
            if (tc && typeof tc === "object") {
              const tcId = (tc as Record<string, unknown>).id;
              if (typeof tcId === "string" && tcId.length > 0) {
                this.closeOutToolCallIds.add(tcId);
              }
            }
          }
        }
        return;
      }
      if (!hasValidToolCalls(rawCalls)) {
        return;
      }
      if (Array.isArray(rawCalls)) {
        for (const tc of rawCalls) {
          if (!tc || typeof tc !== "object") continue;
          const rec = tc as Record<string, unknown>;
          if (typeof rec.name !== "string" || rec.name.trim().length === 0) {
            continue;
          }
          const tcId =
            typeof rec.id === "string" && rec.id.length > 0 ? rec.id : null;
          if (tcId) {
            this.pendingToolCallIds.add(tcId);
            if (msgId) {
              let set = this.pendingToolCallsByMsg.get(msgId);
              if (!set) {
                set = new Set<string>();
                this.pendingToolCallsByMsg.set(msgId, set);
              }
              set.add(tcId);
              this.toolCallToMsgId[tcId] = msgId;
            }
          } else {
            this.hasPendingUnnamedToolCall = true;
          }
        }
      }
      if (payload.execution_started === true || payload.confirmed === true) {
        this.confirmBusinessToolExecution();
      }
      return;
    }

    if (eventType === "tool_call_start") {
      const tcId =
        typeof payload.tool_call_id === "string" &&
        payload.tool_call_id.length > 0
          ? payload.tool_call_id
          : null;
      if (tcId && this.closeOutToolCallIds.has(tcId)) {
        return;
      }
      this.confirmBusinessToolExecution(tcId);
      return;
    }

    if (eventType === "tool_call_result") {
      const tcId =
        typeof payload.tool_call_id === "string" &&
        payload.tool_call_id.length > 0
          ? payload.tool_call_id
          : null;
      if (payload.discarded === true) {
        this.dropPendingToolCall(tcId);
        return;
      }
      if (tcId && this.closeOutToolCallIds.has(tcId)) {
        return;
      }
      this.confirmBusinessToolExecution(tcId);
      return;
    }

    if (eventType === "message_discarded") {
      const discarded = extractDiscardedIds(payload);
      if (discarded.size > 0) {
        for (const id of discarded) {
          this.discardedIds.add(id);
          this.chainIds.delete(id);
          const staleTcs = this.pendingToolCallsByMsg.get(id);
          if (staleTcs) {
            for (const tcId of staleTcs) {
              this.pendingToolCallIds.delete(tcId);
            }
            this.pendingToolCallsByMsg.delete(id);
          }
        }
        this.segments = this.segments.filter(
          (s) => s.msgId === null || !discarded.has(s.msgId),
        );
        if (this.activeMsgId !== null && discarded.has(this.activeMsgId)) {
          this.activeMsgId =
            this.segments.length > 0
              ? this.segments[this.segments.length - 1]!.msgId
              : null;
        }
      }
      return;
    }

    if (
      eventType !== "message_chunk" &&
      eventType !== "text" &&
      eventType !== "content_block_delta" &&
      eventType !== "message"
    ) {
      return;
    }

    if (payload.role === "user") {
      return;
    }

    if (
      typeof payload.content_type === "string" &&
      payload.content_type !== "text"
    ) {
      return;
    }

    const msgId =
      typeof payload.id === "string" && payload.id.length > 0
        ? payload.id
        : null;
    if (payload.images && typeof payload.images === "object" && !Array.isArray(payload.images)) {
      if (msgId) {
        const bucket = this.imagesByMessage[msgId] ?? (this.imagesByMessage[msgId] = {});
        ingestImageMap(bucket, payload.images);
      } else {
        ingestImageMap(this.imageUrlMap, payload.images);
      }
    }

    const delta =
      typeof payload.content === "string"
        ? payload.content
        : typeof payload.text === "string"
          ? payload.text
          : typeof payload.delta === "string"
            ? payload.delta
            : "";

    const finishReason =
      typeof payload.finish_reason === "string" ? payload.finish_reason : null;

    // Legacy tracking (mirrors pre-strict orchestrator behavior exactly).
    if (eventType === "message") {
      if (payload.role === "assistant" && typeof payload.content === "string") {
        this.legacyCurrentSegmentText = payload.content;
      }
    } else {
      if (delta.length > 0) {
        this.legacyCurrentSegmentText += delta;
      }
      if (finishReason === "tool_calls") {
        if (this.legacyCurrentSegmentText.trim().length > 0) {
          this.legacyLastCompletedSegmentText = this.legacyCurrentSegmentText;
          this.legacyCurrentSegmentText = "";
        }
      }
    }

    if (payload.phase === "commentary") {
      return;
    }

    if (msgId && this.discardedIds.has(msgId)) {
      return;
    }

    const continuedFrom =
      typeof payload.continued_from_message_id === "string" &&
      payload.continued_from_message_id.length > 0
        ? payload.continued_from_message_id
        : null;

    if (delta.length > 0) {
      if (eventType === "message") {
        if (continuedFrom && this.chainIds.has(continuedFrom)) {
          this.segments.push({ msgId, text: delta });
          if (msgId) {
            this.chainIds.add(msgId);
            this.activeMsgId = msgId;
          }
        } else {
          this.segments = [{ msgId, text: delta }];
          this.chainIds = new Set(msgId ? [msgId] : []);
          this.activeMsgId = msgId;
        }
      } else if (msgId !== null) {
        if (this.segments.length > 0 && this.activeMsgId === msgId) {
          this.segments[this.segments.length - 1]!.text += delta;
        } else if (continuedFrom && this.chainIds.has(continuedFrom)) {
          this.segments.push({ msgId, text: delta });
          this.chainIds.add(msgId);
          this.activeMsgId = msgId;
        } else {
          this.segments = [{ msgId, text: delta }];
          this.chainIds = new Set([msgId]);
          this.activeMsgId = msgId;
        }
      } else {
        if (this.segments.length > 0 && this.activeMsgId === null) {
          this.segments[this.segments.length - 1]!.text += delta;
        } else {
          this.segments.push({ msgId: null, text: delta });
          this.activeMsgId = null;
        }
      }
    }
  }

  public result(): DeliverableExtractionResult {
    const raw = this.segments.map((s) => s.text).join("");
    let cleaned = stripOpeningThinkingLeak(raw);
    if (cleaned.trim().length === 0) {
      return {
        text: "",
        sawBusinessToolCall: this.sawBusinessToolCall,
      };
    }
    const hasGlobal = Object.keys(this.imageUrlMap).length > 0;
    const hasPerMsg = Object.keys(this.imagesByMessage).length > 0;
    if (hasGlobal || hasPerMsg) {
      let drop = raw.endsWith(cleaned) ? raw.length - cleaned.length : 0;
      const parts: string[] = [];
      for (const seg of this.segments) {
        let piece = seg.text;
        if (drop > 0) {
          if (drop >= piece.length) {
            drop -= piece.length;
            continue;
          }
          piece = piece.slice(drop);
          drop = 0;
        }
        if (piece.length === 0) continue;
        const own = seg.msgId ? this.imagesByMessage[seg.msgId] : undefined;
        const segMap = own
          ? { ...this.imageUrlMap, ...own }
          : this.imageUrlMap;
        parts.push(
          Object.keys(segMap).length > 0
            ? resolveImagePaths(piece, segMap)
            : piece,
        );
      }
      cleaned = parts.join("");
    }
    return {
      text: cleaned.trim().length > 0 ? cleaned : "",
      sawBusinessToolCall: this.sawBusinessToolCall,
    };
  }

  /** Legacy pre-strict text extraction used when ``DALTON_STRICT_DELIVERABLE_TEXT=0``. */
  public legacyResult(): string {
    return this.legacyCurrentSegmentText.trim().length > 0
      ? this.legacyCurrentSegmentText
      : this.legacyLastCompletedSegmentText;
  }
}

export function extractDeliverableTextFromEvents(
  events: Iterable<{ event: string; data: Record<string, unknown> }>,
  imageUrlMap?: Record<string, string>,
): DeliverableExtractionResult {
  const acc = new DeliverableTextAccumulator();
  if (imageUrlMap) {
    acc.setImageUrlMap(imageUrlMap);
  }
  for (const item of events) {
    acc.pushEvent(item.event, item.data);
  }
  return acc.result();
}

export function resolveChannelFallbackText(params: {
  extractedText: string;
  sawBusinessToolCall: boolean;
  sentToCurrentCount: number;
  hasDeliveredCurrentAgentSend?: boolean;
  hasFailedCurrentAgentSend?: boolean;
  strictDeliverableTextEnabled?: boolean;
  legacyText?: string;
}): string {
  if (params.strictDeliverableTextEnabled === false) {
    return (params.legacyText ?? params.extractedText).trim();
  }
  const trimmed = params.extractedText.trim();
  if (trimmed.length > 0) {
    return trimmed;
  }
  if (params.hasDeliveredCurrentAgentSend || params.sentToCurrentCount > 0) {
    return "";
  }
  if (!params.sawBusinessToolCall) {
    return "";
  }
  return params.hasFailedCurrentAgentSend
    ? CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE
    : CHANNEL_EMPTY_AFTER_TOOLS_NOTICE;
}
