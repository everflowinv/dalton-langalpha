import { api, ApiError } from '@/api/client';

export const MAX_CHAT_UPLOAD_FILE_SIZE = 200 * 1024 * 1024; // 200 MiB inclusive
export const MAX_TOTAL_CHAT_UPLOAD_BYTES = 600 * 1024 * 1024; // 600 MiB per message
export const CHAT_UPLOAD_PART_SIZE = 8 * 1024 * 1024; // 8 MiB per chunk
export const MAX_PARALLEL_PARTS = 2;

const PART_REQUEST_TIMEOUT_MS = 120_000;
const MAX_PART_ATTEMPTS = 3;

export interface InitiateChatUploadResponse {
  upload_id: string;
  workspace_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  part_size: number;
  part_count: number;
  state: string;
  sha256?: string | null;
}

export interface ChatUploadPartResponse {
  upload_id: string;
  part_number: number;
  size_bytes: number;
  sha256: string;
  state: string;
}

export interface CompleteChatUploadResponse {
  upload_id: string;
  workspace_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  part_size: number;
  part_count: number;
  state: string;
  sha256?: string | null;
}

export interface UploadChatFileOptions {
  workspaceId?: string | null;
  signal?: AbortSignal;
  onInitiated?: (uploadId: string, resolvedWorkspaceId: string) => void;
  onProgress?: (percent: number) => void;
}

export interface UploadChatFileResult {
  uploadId: string;
  workspaceId: string;
  sizeBytes: number;
  sha256: string | null;
}

class UploadProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UploadProtocolError';
  }
}

function isAbortError(err: unknown, signal?: AbortSignal): boolean {
  if (signal?.aborted) return true;
  if (err && typeof err === 'object') {
    const name = (err as { name?: unknown }).name;
    if (name === 'CanceledError' || name === 'AbortError') return true;
  }
  return false;
}

function isRetryablePartError(err: unknown, signal?: AbortSignal): boolean {
  if (isAbortError(err, signal) || err instanceof UploadProtocolError) return false;
  if (err instanceof ApiError) {
    const status = err.response?.status;
    if (status === undefined) return true; // network / timeout
    return status === 408 || status === 429 || status >= 500;
  }
  return true;
}

function extractMissingParts(err: unknown): number[] | null {
  if (!(err instanceof ApiError) || err.response?.status !== 409) return null;
  const data = err.response.data;
  if (!data || typeof data !== 'object') return null;
  const detail = (data as { detail?: unknown }).detail;
  if (!detail || typeof detail !== 'object') return null;
  const code = (detail as { code?: unknown }).code;
  const missing = (detail as { missing_parts?: unknown }).missing_parts;
  if (code !== 'missing_parts' || !Array.isArray(missing)) return null;
  const nums = missing.filter((n): n is number => typeof n === 'number' && Number.isInteger(n) && n >= 1);
  return nums.length > 0 ? nums : null;
}

function isCompletingInProgress(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.response?.status !== 409) return false;
  const data = err.response.data;
  if (!data || typeof data !== 'object') return false;
  const detail = (data as { detail?: unknown }).detail;
  if (!detail || typeof detail !== 'object') return false;
  return (detail as { code?: unknown }).code === 'completing_in_progress';
}

export function formatUploadErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const data = err.response?.data;
    if (data && typeof data === 'object') {
      const detail = (data as { detail?: unknown }).detail;
      if (typeof detail === 'string' && detail.trim()) {
        return detail.trim();
      }
      if (detail && typeof detail === 'object') {
        const msg = (detail as { message?: unknown }).message;
        if (typeof msg === 'string' && msg.trim()) return msg.trim();
      }
    }
    if (err.message) return err.message;
  }
  if (err instanceof Error && err.message) return err.message;
  return 'Upload failed';
}

export async function sha256Hex(buffer: ArrayBuffer): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null;
  const digest = await subtle.digest('SHA-256', buffer);
  const bytes = new Uint8Array(digest);
  let hex = '';
  for (let i = 0; i < bytes.length; i += 1) {
    hex += bytes[i].toString(16).padStart(2, '0');
  }
  return hex;
}

async function computeBlobSha256(blob: Blob): Promise<string | null> {
  if (!globalThis.crypto?.subtle) return null;
  const buffer = await blob.arrayBuffer();
  return sha256Hex(buffer);
}

function delayWithSignal(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      const err = new Error('Upload canceled');
      err.name = 'CanceledError';
      reject(err);
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      const err = new Error('Upload canceled');
      err.name = 'CanceledError';
      reject(err);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export async function cancelChatUpload(uploadId: string): Promise<void> {
  if (!uploadId) return;
  try {
    await api.delete(`/api/v1/dalton/chat-uploads/${encodeURIComponent(uploadId)}`);
  } catch {
    // Cleanup is best-effort; server expiry sweeps any orphaned state.
  }
}

function validateInitiateResponse(
  session: InitiateChatUploadResponse,
  expectedSizeBytes: number,
): { uploadId: string; resolvedWorkspaceId: string; partSize: number; partCount: number } {
  const rawUploadId = typeof session?.upload_id === 'string' ? session.upload_id.trim() : '';
  const rawWorkspaceId =
    typeof session?.workspace_id === 'string' ? session.workspace_id.trim() : '';
  const sizeValid =
    Number.isInteger(expectedSizeBytes) &&
    expectedSizeBytes > 0 &&
    expectedSizeBytes <= MAX_CHAT_UPLOAD_FILE_SIZE &&
    Number.isInteger(session?.size_bytes) &&
    session.size_bytes === expectedSizeBytes;
  const partSizeValid =
    Number.isInteger(session?.part_size) &&
    session.part_size > 0 &&
    session.part_size <= MAX_CHAT_UPLOAD_FILE_SIZE;
  const expectedPartCount = partSizeValid
    ? Math.max(1, Math.ceil(expectedSizeBytes / session.part_size))
    : -1;
  const partCountValid =
    Number.isInteger(session?.part_count) && session.part_count === expectedPartCount;

  if (!rawUploadId || !rawWorkspaceId || !sizeValid || !partSizeValid || !partCountValid) {
    if (rawUploadId) {
      void cancelChatUpload(rawUploadId);
    }
    throw new UploadProtocolError('Invalid upload session metadata from server');
  }

  return {
    uploadId: rawUploadId,
    resolvedWorkspaceId: rawWorkspaceId,
    partSize: session.part_size,
    partCount: session.part_count,
  };
}

function validatePartResponse(
  part: ChatUploadPartResponse,
  expectedUploadId: string,
  expectedPartNumber: number,
  expectedSizeBytes: number,
): void {
  if (
    !part ||
    part.upload_id !== expectedUploadId ||
    part.part_number !== expectedPartNumber ||
    part.size_bytes !== expectedSizeBytes
  ) {
    throw new UploadProtocolError('Invalid upload part metadata from server');
  }
}

function validateCompletedResponse(
  completed: CompleteChatUploadResponse | null,
  expectedUploadId: string,
  expectedSizeBytes: number,
): CompleteChatUploadResponse {
  if (
    !completed ||
    completed.upload_id !== expectedUploadId ||
    completed.state !== 'ready' ||
    typeof completed.workspace_id !== 'string' ||
    !completed.workspace_id.trim() ||
    !Number.isInteger(completed.size_bytes) ||
    completed.size_bytes !== expectedSizeBytes
  ) {
    throw new UploadProtocolError('Upload completion size or state mismatch');
  }
  return completed;
}

/**
 * Stream a file to the backend multipart relay in bounded slices (default 8 MiB,
 * max 2 concurrent slices) so neither browser nor backend buffers the full file.
 */
export async function uploadChatFile(
  file: File,
  options: UploadChatFileOptions = {},
): Promise<UploadChatFileResult> {
  const { workspaceId, signal, onInitiated, onProgress } = options;
  if (signal?.aborted) {
    const err = new Error('Upload canceled');
    err.name = 'CanceledError';
    throw err;
  }

  onProgress?.(0);

  const initRes = await api.post<InitiateChatUploadResponse>(
    '/api/v1/dalton/chat-uploads',
    {
      workspace_id: workspaceId && workspaceId.trim() ? workspaceId.trim() : 'home',
      filename: file.name || 'attachment.bin',
      size_bytes: file.size,
      mime_type: file.type || 'application/octet-stream',
    },
    { signal },
  );

  const { uploadId, resolvedWorkspaceId, partSize, partCount } = validateInitiateResponse(
    initRes.data,
    file.size,
  );

  onInitiated?.(uploadId, resolvedWorkspaceId);

  const partLoaded = new Array<number>(partCount).fill(0);
  let highestPercent = 0;

  const reportProgress = () => {
    if (!onProgress || file.size <= 0) return;
    let totalLoaded = 0;
    for (let i = 0; i < partLoaded.length; i += 1) {
      totalLoaded += partLoaded[i];
    }
    const rawPct = Math.min(99, Math.floor((Math.min(totalLoaded, file.size) / file.size) * 100));
    if (rawPct > highestPercent) {
      highestPercent = rawPct;
      onProgress(highestPercent);
    }
  };

  const uploadSinglePart = async (partNumber: number): Promise<void> => {
    const idx = partNumber - 1;
    const start = idx * partSize;
    const end = Math.min(file.size, start + partSize);
    const slice = file.slice(start, end);
    const expectedLen = end - start;
    const partSha = await computeBlobSha256(slice);

    for (let attempt = 1; attempt <= MAX_PART_ATTEMPTS; attempt += 1) {
      if (signal?.aborted) {
        const err = new Error('Upload canceled');
        err.name = 'CanceledError';
        throw err;
      }
      partLoaded[idx] = 0;
      try {
        const headers: Record<string, string> = {
          'Content-Type': 'application/octet-stream',
        };
        if (partSha) {
          headers['X-Part-Sha256'] = partSha;
        }
        const partRes = await api.put<ChatUploadPartResponse>(
          `/api/v1/dalton/chat-uploads/${encodeURIComponent(uploadId)}/parts/${partNumber}`,
          slice,
          {
            headers,
            signal,
            timeout: PART_REQUEST_TIMEOUT_MS,
            onUploadProgress: (progress) => {
              partLoaded[idx] = Math.min(expectedLen, Math.max(0, progress.loaded));
              reportProgress();
            },
          },
        );
        validatePartResponse(partRes.data, uploadId, partNumber, expectedLen);
        partLoaded[idx] = expectedLen;
        reportProgress();
        return;
      } catch (err) {
        partLoaded[idx] = 0;
        if (attempt >= MAX_PART_ATTEMPTS || !isRetryablePartError(err, signal)) {
          throw err;
        }
        await delayWithSignal(250 * attempt, signal);
      }
    }
  };

  const uploadPartsList = async (partNumbers: number[]): Promise<void> => {
    let cursor = 0;
    const concurrency = Math.min(MAX_PARALLEL_PARTS, partNumbers.length);
    const workers = Array.from({ length: concurrency }, async () => {
      while (cursor < partNumbers.length) {
        if (signal?.aborted) {
          const err = new Error('Upload canceled');
          err.name = 'CanceledError';
          throw err;
        }
        const current = partNumbers[cursor];
        cursor += 1;
        await uploadSinglePart(current);
      }
    });
    await Promise.all(workers);
  };

  try {
    const allParts = Array.from({ length: partCount }, (_, i) => i + 1);
    await uploadPartsList(allParts);

    let completed: CompleteChatUploadResponse | null = null;
    try {
      const completeRes = await api.post<CompleteChatUploadResponse>(
        `/api/v1/dalton/chat-uploads/${encodeURIComponent(uploadId)}/complete`,
        {},
        { signal, timeout: PART_REQUEST_TIMEOUT_MS },
      );
      completed = completeRes.data;
    } catch (err) {
      const missingParts = extractMissingParts(err);
      if (missingParts) {
        await uploadPartsList(missingParts);
        const retryComplete = await api.post<CompleteChatUploadResponse>(
          `/api/v1/dalton/chat-uploads/${encodeURIComponent(uploadId)}/complete`,
          {},
          { signal, timeout: PART_REQUEST_TIMEOUT_MS },
        );
        completed = retryComplete.data;
      } else if (isCompletingInProgress(err)) {
        for (let poll = 0; poll < 8; poll += 1) {
          await delayWithSignal(400, signal);
          const statusRes = await api.get<CompleteChatUploadResponse>(
            `/api/v1/dalton/chat-uploads/${encodeURIComponent(uploadId)}`,
            { signal },
          );
          if (statusRes.data.state === 'ready') {
            completed = statusRes.data;
            break;
          }
        }
        if (!completed) throw err;
      } else {
        throw err;
      }
    }

    const verifiedCompleted = validateCompletedResponse(completed, uploadId, file.size);

    onProgress?.(100);
    return {
      uploadId: verifiedCompleted.upload_id,
      workspaceId: verifiedCompleted.workspace_id,
      sizeBytes: verifiedCompleted.size_bytes,
      sha256: verifiedCompleted.sha256 ?? null,
    };
  } catch (err) {
    if (isAbortError(err, signal) || err instanceof UploadProtocolError) {
      void cancelChatUpload(uploadId);
    }
    throw err;
  }
}
