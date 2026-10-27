/**
 * Workspace file endpoints (list/read/write/delete/backup/upload).
 */
import { api } from '@/api/client';
import { isPlatformMode } from '@/config/hostMode';
import { isDaltonAuth } from '@/config/daltonAuth'; // DALTON-HOOK(dalton-auth): use authenticated blob download when Dalton auth is enabled
import { withDownloadNotice, workspaceDownloadKey, type DownloadProgressReport } from '../downloadNotice';
import type { FileRefResolution } from '../../components/filePanel/types';
import type { BackupResponse, BackupStatusResponse } from '@/types/api';

/**
 * Resolve a file reference to one workspace path in a single server lookup.
 * @param candidates - readings of the reference, most likely first
 * @param recentWrites - this thread's Write/Edit paths, newest first
 */
export async function resolveWorkspaceFile(
  workspaceId: string,
  candidates: string[],
  recentWrites: string[] = [],
): Promise<FileRefResolution> {
  const { data } = await api.post(`/api/v1/workspaces/${workspaceId}/files/resolve`, {
    candidates,
    recent_writes: recentWrites,
  });
  return data as FileRefResolution;
}

/**
 * Read a text file from workspace sandbox
 * @param {string} workspaceId
 * @param {string} filePath - e.g. "results/report.md"
 */
export async function readWorkspaceFile(workspaceId: string, filePath: string) {
  const { data } = await api.get(`/api/v1/workspaces/${workspaceId}/files/read`, {
    params: { path: filePath },
  });
  return data; // { workspace_id, path, content, mime, truncated }
}

/**
 * Download a file from workspace sandbox (returns blob URL)
 * @param {string} workspaceId
 * @param {string} filePath
 * @returns {Promise<string>} Blob URL for the file
 */
export async function downloadWorkspaceFile(
  workspaceId: string,
  filePath: string,
  onProgress?: DownloadProgressReport,
) {
  const response = await api.get(`/api/v1/workspaces/${workspaceId}/files/download`, {
    params: { path: filePath },
    responseType: 'blob',
    onDownloadProgress: onProgress
      ? (event) => { if (event.total) onProgress(event.loaded / event.total); }
      : undefined,
  });
  return URL.createObjectURL(response.data as Blob);
}

/**
 * Download a file from workspace sandbox as ArrayBuffer (for client-side parsing)
 * @param {string} workspaceId
 * @param {string} filePath
 * @returns {Promise<ArrayBuffer>}
 */
export async function downloadWorkspaceFileAsArrayBuffer(workspaceId: string, filePath: string) {
  const response = await api.get(`/api/v1/workspaces/${workspaceId}/files/download`, {
    params: { path: filePath },
    responseType: 'arraybuffer',
  });
  return response.data as ArrayBuffer;
}

/**
 * Trigger file download in browser
 * @param {string} workspaceId
 * @param {string} filePath
 */
export function triggerFileDownload(workspaceId: string, filePath: string): Promise<void> {
  const fileName = filePath.split('/').pop() || 'download';
  return withDownloadNotice(workspaceDownloadKey(workspaceId, filePath), fileName, (report, current) =>
    saveWorkspaceFile(workspaceId, filePath, fileName, report, current),
  );
}

async function saveWorkspaceFile(
  workspaceId: string,
  filePath: string,
  fileName: string,
  report: DownloadProgressReport,
  current: () => boolean,
) {
  // A large file is handed out as a short-lived storage link the browser
  // streams to disk itself; buffering it as a blob would hold the whole file
  // in tab memory. A null url means the file is small enough to fetch here.
  const { data } = await api.get<{ url: string | null }>(
    `/api/v1/workspaces/${workspaceId}/files/download-url`,
    { params: { path: filePath } },
  );
  // Without a bearer token the download route itself is a plain GET, so the
  // browser can stream it to disk too; with one, only a blob can carry it.
  // DALTON-HOOK(dalton-auth): direct GET carries no bearer token; fetch blob when Dalton auth is enabled
  const direct = data?.url ?? (isPlatformMode || isDaltonAuth ? null : directDownloadUrl(workspaceId, filePath));
  const href = direct ?? (await downloadWorkspaceFile(workspaceId, filePath, report));
  // Signed out while this was being prepared: the file is the last account's.
  if (!current()) {
    if (!direct) URL.revokeObjectURL(href);
    return;
  }
  const a = document.createElement('a');
  a.href = href;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  if (!direct) URL.revokeObjectURL(href);
}

function directDownloadUrl(workspaceId: string, filePath: string): string {
  return api.getUri({
    url: `/api/v1/workspaces/${workspaceId}/files/download`,
    params: { path: filePath, attachment: true },
  });
}

/** Back up the sandbox's files so they outlive it. */
export async function backupWorkspaceFiles(workspaceId: string): Promise<BackupResponse> {
  const { data } = await api.post<BackupResponse>(`/api/v1/workspaces/${workspaceId}/files/backup`);
  return data;
}

/** Which sandbox files match their backup, differ from it, or have none. */
export async function getBackupStatus(workspaceId: string): Promise<BackupStatusResponse> {
  const { data } = await api.get<BackupStatusResponse>(`/api/v1/workspaces/${workspaceId}/files/backup-status`);
  return data;
}

/**
 * Write full file content to a sandbox file
 * @param {string} workspaceId
 * @param {string} filePath - e.g. "results/report.py"
 * @param {string} content - File content to write
 * @returns {Promise<Object>} { workspace_id, path, size }
 */
export async function writeWorkspaceFile(workspaceId: string, filePath: string, content: string) {
  const { data } = await api.put(`/api/v1/workspaces/${workspaceId}/files/write`,
    { content },
    { params: { path: filePath } }
  );
  return data;
}

/**
 * Read a file without line-limit pagination (for edit mode)
 * @param {string} workspaceId
 * @param {string} filePath
 * @returns {Promise<Object>} { workspace_id, path, content, mime }
 */
export async function readWorkspaceFileFull(workspaceId: string, filePath: string) {
  const { data } = await api.get(`/api/v1/workspaces/${workspaceId}/files/read`, {
    params: { path: filePath, unlimited: true },
  });
  return data;
}

export async function deleteWorkspaceFiles(workspaceId: string, paths: string[]) {
  const { data } = await api.delete(`/api/v1/workspaces/${workspaceId}/files`, {
    data: { paths },
  });
  return data;
}

// --- Sandbox ---

/** Sends the file in one request through the backend; `beginWorkspaceUpload` says when this is the path. */
export async function uploadWorkspaceFile(
  workspaceId: string,
  file: File,
  destPath: string | null = null,
  onProgress: ((percent: number) => void) | null = null,
  signal?: AbortSignal,
) {
  const formData = new FormData();
  formData.append('file', file);
  const params = destPath ? { path: destPath } : {};
  const { data } = await api.post(
    `/api/v1/workspaces/${workspaceId}/files/upload`,
    formData,
    {
      params,
      signal,
      onUploadProgress: onProgress
        ? (e) => onProgress(Math.round((e.loaded * 100) / (e.total || 1)))
        : undefined,
    }
  );
  return data;
}

export type UploadBegin =
  | { mode: 'relay' }
  | {
    mode: 'direct';
    upload_id: string;
    /** Where the file will land, relative to the workspace folder. */
    path: string;
    part_size: number;
    part_count: number;
    expires_at: string;
  };

export interface SignedUploadPart {
  part_number: number;
  offset: number;
  size: number;
  url: string;
}

export interface UploadStatus {
  upload_id: string;
  state: 'uploading' | 'completing' | 'pulling' | 'done' | 'failed' | 'aborted';
  path: string;
  size: number;
  error: string | null;
}

const uploadsPath = (workspaceId: string, uploadId?: string) =>
  `/api/v1/workspaces/${workspaceId}/files/uploads${uploadId ? `/${uploadId}` : ''}`;

export async function beginWorkspaceUpload(
  workspaceId: string,
  body: { path: string; size: number; sha256?: string },
  signal?: AbortSignal,
): Promise<UploadBegin> {
  const { data } = await api.post(uploadsPath(workspaceId), body, { signal });
  return data as UploadBegin;
}

export async function signWorkspaceUploadParts(
  workspaceId: string,
  uploadId: string,
  partNumbers: number[],
  signal?: AbortSignal,
): Promise<{ parts: SignedUploadPart[]; expires_in: number }> {
  const { data } = await api.post(`${uploadsPath(workspaceId, uploadId)}/parts`, { part_numbers: partNumbers }, { signal });
  return data;
}

export async function completeWorkspaceUpload(
  workspaceId: string,
  uploadId: string,
  signal?: AbortSignal,
): Promise<UploadStatus> {
  const { data } = await api.post(`${uploadsPath(workspaceId, uploadId)}/complete`, undefined, { signal });
  return data as UploadStatus;
}

export async function getWorkspaceUpload(
  workspaceId: string,
  uploadId: string,
  signal?: AbortSignal,
): Promise<UploadStatus> {
  const { data } = await api.get(uploadsPath(workspaceId, uploadId), { signal });
  return data as UploadStatus;
}

export async function cancelWorkspaceUpload(workspaceId: string, uploadId: string): Promise<void> {
  await api.delete(uploadsPath(workspaceId, uploadId));
}

/**
 * Ends an upload as the page goes away. A keepalive request outlives the page
 * that sent it, where the API client's would be cut off with the page; the
 * caller hands over the auth headers, since there is no time left to fetch them.
 */
export function cancelWorkspaceUploadOnUnload(
  workspaceId: string,
  uploadId: string,
  headers: Record<string, string>,
): void {
  try {
    void fetch(api.getUri({ url: uploadsPath(workspaceId, uploadId) }), {
      method: 'DELETE',
      keepalive: true,
      headers,
    }).catch(() => {});
  } catch {
    /* best effort: the row still expires */
  }
}

// --- Vault Secrets ---
