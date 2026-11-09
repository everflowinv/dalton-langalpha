/**
 * Workspace outbound file download helper (`src/shared/workspace-download.ts`).
 *
 * Design §1.3.1 & §4.1:
 * Delegates to the existing `BackendClient.downloadWorkspaceFile` in
 * `src/backend-client.ts` rather than duplicating HTTP calls, while enforcing
 * path safety and the 20 MB in-memory attachment limit.
 */

import { posix } from "node:path";
import type { BackendClient } from "../backend-client.js";
import { MAX_ATTACHMENT_BYTES } from "../types.js";

export interface DownloadOutboundWorkspaceFileOptions {
  backendClient: Pick<BackendClient, "downloadWorkspaceFile">;
  userId: string;
  workspaceId: string;
  workspacePath: string;
  maxBytes?: number;
}

export interface DownloadOutboundWorkspaceFileResult {
  buffer: Buffer;
  filename: string;
  mimeType: string;
  sizeBytes: number;
}

export function normalizeSafeWorkspacePath(rawPath: string): string {
  const trimmed = rawPath.trim();
  if (!trimmed || trimmed.includes("\0")) {
    throw new Error("Invalid workspace file path");
  }
  const slashNormalized = trimmed.replace(/\\/g, "/");
  const segments = slashNormalized.split("/").filter(Boolean);
  if (segments.length === 0 || segments.some((seg) => seg === "..")) {
    throw new Error(`Disallowed workspace file path: ${rawPath}`);
  }
  return posix.normalize(slashNormalized);
}

export async function downloadWorkspaceFileToBuffer(
  options: DownloadOutboundWorkspaceFileOptions,
): Promise<DownloadOutboundWorkspaceFileResult> {
  const normalizedPath = normalizeSafeWorkspacePath(options.workspacePath);
  const maxBytes = Math.min(
    MAX_ATTACHMENT_BYTES,
    Math.max(1, options.maxBytes ?? MAX_ATTACHMENT_BYTES),
  );

  const downloaded = await options.backendClient.downloadWorkspaceFile(
    options.userId,
    options.workspaceId,
    normalizedPath,
  );

  if (downloaded.content.byteLength > maxBytes) {
    throw new Error(
      `Downloaded workspace file "${normalizedPath}" (${downloaded.content.byteLength} bytes) exceeds limit of ${maxBytes} bytes`,
    );
  }

  const filename = posix.basename(normalizedPath) || "attachment.bin";

  return {
    buffer: downloaded.content,
    filename,
    mimeType: downloaded.mimeType || "application/octet-stream",
    sizeBytes: downloaded.content.byteLength,
  };
}
