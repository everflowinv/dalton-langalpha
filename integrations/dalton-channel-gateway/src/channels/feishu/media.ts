/**
 * Feishu inbound attachment download (`im.messageResource.get`) and outbound
 * image/file upload + delivery (`im.image.create` / `im.file.create`).
 *
 * Adapted from `.agy/ref_openclaw_channels/feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs`
 * (lines 1709–2437).
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.1 & §4.2 row 6 (`src/channels/feishu/media.ts`):
 * - All media payloads are buffered in memory (`Buffer`), never written to disk.
 * - Image upload cap: 10 MB (`FEISHU_MAX_IMAGE_UPLOAD_BYTES`).
 * - File upload/download cap: 20 MB (`FEISHU_MAX_FILE_UPLOAD_BYTES`).
 * - Excludes `ffmpeg`/`ffprobe` transcoding/probing and `withFeishuSendContext`.
 */

import path from "node:path";
import type * as Lark from "@larksuiteoapi/node-sdk";
import { readStreamWithLimit, SafeFetchError } from "../../shared/safe-fetch.js";
import { MAX_ATTACHMENT_BYTES } from "../../types.js";
import { ChannelDeliveryError } from "../adapter.js";
import {
  classifyFeishuDeliveryError,
  type FeishuMessageSendResult,
  resolveFeishuSendTarget,
  sendReplyOrFallbackDirect,
  toFeishuMessageSendResult,
} from "./send.js";

export const FEISHU_MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024; // 10 MB
export const FEISHU_MAX_FILE_UPLOAD_BYTES = MAX_ATTACHMENT_BYTES; // 20 MB

export type FeishuUploadFileType =
  | "opus"
  | "mp4"
  | "pdf"
  | "doc"
  | "xls"
  | "ppt"
  | "stream";

const FEISHU_IMAGE_EXTENSIONS = new Set<string>([
  ".jpg",
  ".jpeg",
  ".png",
  ".webp",
  ".gif",
  ".tiff",
  ".tif",
  ".bmp",
  ".ico",
]);

const FEISHU_NON_RASTER_IMAGE_EXTENSIONS = new Set<string>([".svg", ".svgz"]);

const ASCII_CONTROL_CHAR_RANGE = "\u0000-\u001f\u007f";
const ASCII_CONTROL_CHARS_RE = new RegExp(`[${ASCII_CONTROL_CHAR_RANGE}]`, "g");
const PATH_SEPARATORS_RE = /[/\\]+/g;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function decodeDispositionFileName(rawFileName: string): string {
  const trimmed = rawFileName.trim().replace(/^["']|["']$/g, "");
  if (!trimmed) {
    return "";
  }
  const rfc5987Match =
    /^(?:UTF-8|utf-8)''(.+)$/.exec(trimmed) ??
    /^[a-zA-Z0-9-]+'[a-zA-Z0-9-]*'(.+)$/.exec(trimmed);
  const candidate = rfc5987Match ? rfc5987Match[1]! : trimmed;
  try {
    return decodeURIComponent(candidate);
  } catch {
    return candidate;
  }
}

export function recoverUtf8FileNameFromLatin1Header(
  rawFileName: string,
): string {
  if (!rawFileName || !/[\u0080-\u00ff]/.test(rawFileName)) {
    return rawFileName;
  }
  if (/[\u0100-\uffff]/.test(rawFileName)) {
    return rawFileName;
  }
  try {
    const decoded = Buffer.from(rawFileName, "latin1").toString("utf8");
    if (!decoded || decoded.includes("\ufffd")) {
      return rawFileName;
    }
    return decoded;
  } catch {
    return rawFileName;
  }
}

export function readHeaderFileName(headers: unknown): string | undefined {
  if (!isRecord(headers)) {
    return undefined;
  }
  const dispositionEntry = Object.entries(headers).find(
    ([key]) => key.toLowerCase() === "content-disposition",
  );
  const rawDisposition =
    typeof dispositionEntry?.[1] === "string"
      ? dispositionEntry[1]
      : Array.isArray(dispositionEntry?.[1]) &&
          typeof dispositionEntry[1][0] === "string"
        ? dispositionEntry[1][0]
        : undefined;
  if (!rawDisposition) {
    return undefined;
  }
  const starMatch = /filename\*\s*=\s*([^;]+)/i.exec(rawDisposition);
  if (starMatch?.[1]) {
    const decoded = decodeDispositionFileName(starMatch[1]);
    if (decoded) {
      return recoverUtf8FileNameFromLatin1Header(decoded);
    }
  }
  const plainMatch = /filename\s*=\s*(?:"([^"]+)"|'([^']+)'|([^;\s]+))/i.exec(
    rawDisposition,
  );
  const candidate = plainMatch?.[1] ?? plainMatch?.[2] ?? plainMatch?.[3];
  if (!candidate) {
    return undefined;
  }
  return recoverUtf8FileNameFromLatin1Header(
    decodeDispositionFileName(candidate),
  );
}

/**
 * Sanitizes an outbound filename before multipart upload to Feishu.
 */
export function sanitizeFileNameForUpload(fileName: string): string {
  const cleaned = recoverUtf8FileNameFromLatin1Header(
    decodeDispositionFileName(fileName),
  )
    .replace(ASCII_CONTROL_CHARS_RE, "_")
    .replace(PATH_SEPARATORS_RE, "_")
    .trim();
  return cleaned || "file.bin";
}

export function detectFileType(
  fileName: string,
  mimeType?: string,
): FeishuUploadFileType {
  const ext = path.extname(fileName).toLowerCase();
  switch (ext) {
    case ".opus":
    case ".ogg":
      return "opus";
    case ".mp4":
    case ".mov":
    case ".avi":
      return "mp4";
    case ".pdf":
      return "pdf";
    case ".doc":
    case ".docx":
      return "doc";
    case ".xls":
    case ".xlsx":
    case ".csv":
      return "xls";
    case ".ppt":
    case ".pptx":
      return "ppt";
    default:
      break;
  }
  const mime = mimeType?.toLowerCase().split(";")[0]?.trim() ?? "";
  if (mime === "application/pdf") {
    return "pdf";
  }
  if (mime === "video/mp4") {
    return "mp4";
  }
  if (mime === "audio/ogg" || mime === "audio/opus") {
    return "opus";
  }
  return "stream";
}

export function resolveFeishuOutboundMediaKind(params: {
  fileName?: string;
  contentType?: string;
  byteLength?: number;
}): "image" | "file" {
  if (
    typeof params.byteLength === "number" &&
    params.byteLength > FEISHU_MAX_IMAGE_UPLOAD_BYTES
  ) {
    return "file";
  }
  const normalizedMime = params.contentType
    ?.toLowerCase()
    .split(";")[0]
    ?.trim();
  const ext = params.fileName ? path.extname(params.fileName).toLowerCase() : "";
  if (
    normalizedMime === "image/svg+xml" ||
    FEISHU_NON_RASTER_IMAGE_EXTENSIONS.has(ext)
  ) {
    return "file";
  }
  if (normalizedMime?.startsWith("image/")) {
    return "image";
  }
  if (ext && FEISHU_IMAGE_EXTENSIONS.has(ext)) {
    return "image";
  }
  return "file";
}

async function readFeishuResourceResponseBuffer(
  response: unknown,
  maxBytes: number,
): Promise<{
  buffer: Buffer;
  contentType?: string;
  fileName?: string;
}> {
  if (Buffer.isBuffer(response)) {
    if (response.byteLength > maxBytes) {
      throw new Error(
        `Feishu attachment size (${response.byteLength} bytes) exceeds maximum limit (${maxBytes} bytes)`,
      );
    }
    return { buffer: response };
  }
  if (response instanceof ArrayBuffer) {
    if (response.byteLength > maxBytes) {
      throw new Error(
        `Feishu attachment size (${response.byteLength} bytes) exceeds maximum limit (${maxBytes} bytes)`,
      );
    }
    return { buffer: Buffer.from(response) };
  }
  if (!isRecord(response)) {
    throw new Error("Feishu messageResource.get returned unexpected response");
  }

  const headers = isRecord(response.headers) ? response.headers : undefined;
  const contentTypeEntry = headers
    ? Object.entries(headers).find(
        ([k]) => k.toLowerCase() === "content-type",
      )
    : undefined;
  const contentType =
    typeof contentTypeEntry?.[1] === "string"
      ? contentTypeEntry[1]
      : typeof response.contentType === "string"
        ? response.contentType
        : undefined;
  const fileName =
    readHeaderFileName(headers) ??
    (typeof response.fileName === "string" ? response.fileName : undefined);

  if (Buffer.isBuffer(response.data)) {
    if (response.data.byteLength > maxBytes) {
      throw new SafeFetchError(
        "size_exceeded",
        `Feishu attachment size (${response.data.byteLength} bytes) exceeds maximum limit (${maxBytes} bytes)`,
      );
    }
    return { buffer: response.data, contentType, fileName };
  }
  if (response.data instanceof ArrayBuffer) {
    if (response.data.byteLength > maxBytes) {
      throw new SafeFetchError(
        "size_exceeded",
        `Feishu attachment size (${response.data.byteLength} bytes) exceeds maximum limit (${maxBytes} bytes)`,
      );
    }
    return { buffer: Buffer.from(response.data), contentType, fileName };
  }
  if (typeof response.getReadableStream === "function") {
    const stream = (
      response.getReadableStream as () => AsyncIterable<unknown>
    )();
    const buffer = await readStreamWithLimit(stream, { maxBytes });
    return { buffer, contentType, fileName };
  }
  if (typeof response.writeFile === "function" && isRecord(response.data) && Symbol.asyncIterator in response.data) {
    const buffer = await readStreamWithLimit(
      response.data as unknown as AsyncIterable<unknown>,
      { maxBytes },
    );
    return { buffer, contentType, fileName };
  }
  if (Symbol.asyncIterator in response) {
    const buffer = await readStreamWithLimit(
      response as unknown as AsyncIterable<unknown>,
      { maxBytes },
    );
    return { buffer, contentType, fileName };
  }

  throw new Error(
    "Feishu messageResource.get response does not provide a readable buffer or stream",
  );
}

function isFeishuHttpStatus(err: unknown, status: number): boolean {
  if (!isRecord(err)) {
    return false;
  }
  const response = isRecord(err.response) ? err.response : undefined;
  return (
    response?.status === status ||
    err.status === status ||
    err.httpStatus === status
  );
}

/**
 * Downloads an inbound Feishu message attachment (`image` or `file`) into an
 * in-memory `Buffer` while enforcing `maxBytes` (default 20 MB).
 * Retries once with `type: "media"` when `type: "file"` fails with HTTP 502
 * (`reply-delivery-result-iCCrdxE1.mjs:2673-2681`).
 */
export async function downloadMessageResourceFeishu(
  client: Lark.Client,
  params: {
    messageId: string;
    fileKey: string;
    type: "image" | "file";
    maxBytes?: number;
    originalFilename?: string;
    originalMimeType?: string;
  },
): Promise<{
  buffer: Buffer;
  filename: string;
  mimeType: string;
}> {
  const maxBytes = Math.min(
    FEISHU_MAX_FILE_UPLOAD_BYTES,
    Math.max(1, params.maxBytes ?? FEISHU_MAX_FILE_UPLOAD_BYTES),
  );

  let response: unknown;
  try {
    response = await client.im.messageResource.get({
      path: {
        message_id: params.messageId,
        file_key: params.fileKey,
      },
      params: {
        type: params.type,
      },
    });
  } catch (err) {
    if (params.type === "file" && isFeishuHttpStatus(err, 502)) {
      try {
        response = await client.im.messageResource.get({
          path: {
            message_id: params.messageId,
            file_key: params.fileKey,
          },
          params: {
            type: "media" as unknown as "file",
          },
        });
      } catch {
        throw err;
      }
    } else {
      throw err;
    }
  }

  const { buffer, contentType, fileName } =
    await readFeishuResourceResponseBuffer(response, maxBytes);

  const defaultExt = params.type === "image" ? ".png" : ".bin";
  const resolvedFilename = sanitizeFileNameForUpload(
    fileName || params.originalFilename || `${params.fileKey}${defaultExt}`,
  );
  const resolvedMimeType =
    contentType ||
    params.originalMimeType ||
    (params.type === "image" ? "image/png" : "application/octet-stream");

  return {
    buffer,
    filename: resolvedFilename,
    mimeType: resolvedMimeType,
  };
}

export async function uploadImageFeishu(
  client: Lark.Client,
  params: {
    image: Buffer;
    imageType?: "message" | "avatar";
  },
): Promise<{ imageKey: string }> {
  if (params.image.byteLength > FEISHU_MAX_IMAGE_UPLOAD_BYTES) {
    throw new ChannelDeliveryError(
      `Feishu image exceeds ${FEISHU_MAX_IMAGE_UPLOAD_BYTES} bytes (${params.image.byteLength} bytes)`,
      { outcome: "failed", code: "not_allowed" },
    );
  }

  try {
    const response = (await client.im.image.create({
      data: {
        image_type: params.imageType ?? "message",
        image: params.image,
      },
    })) as {
      code?: number;
      msg?: string;
      image_key?: string;
      data?: { image_key?: string };
    };

    if (response.code !== undefined && response.code !== 0) {
      throw classifyFeishuDeliveryError(
        { response: { status: 200, data: response } },
        "Feishu image upload failed",
        { stage: "upload" },
      );
    }

    const imageKey =
      response.image_key?.trim() ?? response.data?.image_key?.trim();
    if (!imageKey) {
      throw new ChannelDeliveryError(
        "Feishu image upload failed: no image_key returned",
        { outcome: "failed", code: "unavailable" },
      );
    }
    return { imageKey };
  } catch (err) {
    throw classifyFeishuDeliveryError(err, "Feishu image upload failed", {
      stage: "upload",
    });
  }
}

export async function uploadFileFeishu(
  client: Lark.Client,
  params: {
    file: Buffer;
    fileName: string;
    fileType: FeishuUploadFileType;
    duration?: number;
  },
): Promise<{ fileKey: string }> {
  if (params.file.byteLength > FEISHU_MAX_FILE_UPLOAD_BYTES) {
    throw new ChannelDeliveryError(
      `Feishu file "${params.fileName}" exceeds ${FEISHU_MAX_FILE_UPLOAD_BYTES} bytes (${params.file.byteLength} bytes)`,
      { outcome: "failed", code: "not_allowed" },
    );
  }

  const safeFileName = sanitizeFileNameForUpload(params.fileName);

  try {
    const response = (await client.im.file.create({
      data: {
        file_type: params.fileType,
        file_name: safeFileName,
        file: params.file,
        ...(params.duration !== undefined ? { duration: params.duration } : {}),
      },
    })) as {
      code?: number;
      msg?: string;
      file_key?: string;
      data?: { file_key?: string };
    };

    if (response.code !== undefined && response.code !== 0) {
      throw classifyFeishuDeliveryError(
        { response: { status: 200, data: response } },
        "Feishu file upload failed",
        { stage: "upload" },
      );
    }

    const fileKey =
      response.file_key?.trim() ?? response.data?.file_key?.trim();
    if (!fileKey) {
      throw new ChannelDeliveryError(
        "Feishu file upload failed: no file_key returned",
        { outcome: "failed", code: "unavailable" },
      );
    }
    return { fileKey };
  } catch (err) {
    throw classifyFeishuDeliveryError(err, "Feishu file upload failed", {
      stage: "upload",
    });
  }
}

export async function sendMediaFeishu(
  client: Lark.Client,
  params: {
    to: string;
    buffer: Buffer;
    fileName: string;
    mimeType?: string;
    replyToMessageId?: string | null;
    replyInThread?: boolean;
  },
): Promise<FeishuMessageSendResult> {
  if (params.buffer.byteLength > FEISHU_MAX_FILE_UPLOAD_BYTES) {
    throw new ChannelDeliveryError(
      `Feishu outbound attachment "${params.fileName}" exceeds maximum 20 MB limit (${params.buffer.byteLength} bytes)`,
      { outcome: "failed", code: "not_allowed" },
    );
  }

  const { target, receiveIdType, replyToMessageId, replyInThread } =
    resolveFeishuSendTarget({
      to: params.to,
      replyToMessageId: params.replyToMessageId,
      replyInThread: params.replyInThread,
    });

  const mediaKind = resolveFeishuOutboundMediaKind({
    fileName: params.fileName,
    contentType: params.mimeType,
    byteLength: params.buffer.byteLength,
  });

  if (mediaKind === "image") {
    const { imageKey } = await uploadImageFeishu(client, {
      image: params.buffer,
      imageType: "message",
    });
    const response = await sendReplyOrFallbackDirect(client, {
      replyToMessageId,
      replyInThread,
      content: JSON.stringify({ image_key: imageKey }),
      msgType: "image",
      target,
      receiveIdType,
    });
    return toFeishuMessageSendResult(response, target);
  }

  const fileType = detectFileType(params.fileName, params.mimeType);
  const { fileKey } = await uploadFileFeishu(client, {
    file: params.buffer,
    fileName: params.fileName,
    fileType,
  });
  const response = await sendReplyOrFallbackDirect(client, {
    replyToMessageId,
    replyInThread,
    content: JSON.stringify({ file_key: fileKey }),
    msgType: "file",
    target,
    receiveIdType,
  });
  return toFeishuMessageSendResult(response, target);
}
