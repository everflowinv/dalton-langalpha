/**
 * Workspace inbound attachment upload helper (`src/shared/workspace-upload.ts`).
 *
 * Design §1.3.1 & §4.1:
 * Delegates to the existing `BackendClient` methods (`ensureHomeWorkspace`,
 * `startWorkspace`, `uploadWorkspaceFile`) in `src/backend-client.ts` rather
 * than duplicating HTTP calls.
 */

import type { BackendClient } from "../backend-client.js";
import { type ChannelPlatform, MAX_ATTACHMENT_BYTES } from "../types.js";

export interface UploadInboundAttachmentOptions {
  backendClient: Pick<
    BackendClient,
    "ensureHomeWorkspace" | "startWorkspace" | "uploadWorkspaceFile"
  >;
  userId: string;
  workspaceId?: string | null;
  platform: ChannelPlatform | string;
  messageId: string;
  filename: string;
  content: Buffer;
  mimeType?: string;
  maxBytes?: number;
}

export interface UploadInboundAttachmentResult {
  workspaceId: string;
  workspacePath: string;
  safeFilename: string;
  sizeBytes: number;
}

export function sanitizeWorkspacePathSegment(
  raw: string,
  fallback: string,
): string {
  const cleaned = raw
    .trim()
    .replace(/[/\\:*?"<>|\x00-\x1F]+/g, "_")
    .replace(/\.{2,}/g, "_")
    .replace(/^_+|_+$/g, "");
  return cleaned.length > 0 ? cleaned.slice(0, 120) : fallback;
}

export function buildInboundAttachmentWorkspacePath(params: {
  platform: string;
  messageId: string;
  filename: string;
}): { workspacePath: string; safeFilename: string } {
  const safePlatform = sanitizeWorkspacePathSegment(params.platform, "channel");
  const safeMessageId = sanitizeWorkspacePathSegment(params.messageId, "msg");
  const safeFilename = sanitizeWorkspacePathSegment(params.filename, "attachment.bin");
  return {
    workspacePath: `uploads/${safePlatform}/${safeMessageId}/${safeFilename}`,
    safeFilename,
  };
}

export async function uploadAttachmentToWorkspace(
  options: UploadInboundAttachmentOptions,
): Promise<UploadInboundAttachmentResult> {
  const maxBytes = Math.min(
    MAX_ATTACHMENT_BYTES,
    Math.max(1, options.maxBytes ?? MAX_ATTACHMENT_BYTES),
  );
  if (options.content.byteLength > maxBytes) {
    throw new Error(
      `Attachment size (${options.content.byteLength} bytes) exceeds maximum allowed limit (${maxBytes} bytes)`,
    );
  }

  let workspaceId = options.workspaceId?.trim() || "";
  if (!workspaceId) {
    const home = await options.backendClient.ensureHomeWorkspace(options.userId);
    workspaceId = home.workspace_id;
  }

  await options.backendClient.startWorkspace(options.userId, workspaceId);

  const { workspacePath, safeFilename } = buildInboundAttachmentWorkspacePath({
    platform: options.platform,
    messageId: options.messageId,
    filename: options.filename,
  });

  await options.backendClient.uploadWorkspaceFile(
    options.userId,
    workspaceId,
    workspacePath,
    safeFilename,
    options.content,
    options.mimeType || "application/octet-stream",
  );

  return {
    workspaceId,
    workspacePath,
    safeFilename,
    sizeBytes: options.content.byteLength,
  };
}
