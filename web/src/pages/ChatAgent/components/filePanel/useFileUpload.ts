import React, { useCallback, useEffect, useRef, useState } from 'react';
// DALTON-HOOK(multi-file-upload): i18n drives the batch progress and
// failure-summary strings shown while a multi-file upload runs.
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { holdReload } from '@/lib/reloadHolds';
import { uploadToWorkspace, UploadSendError } from '../../utils/workspaceUpload';

function uploadErrorMessage(err: unknown, file: globalThis.File, t: TFunction): string {
  if (err instanceof UploadSendError) return t('filePanel.uploadSendFailed');
  const e = err as { response?: { status?: number; data?: { detail?: unknown } }; message?: string };
  const detail = e?.response?.data?.detail;
  // The server's 413 names its own limit; one without a detail came from
  // something in front of it, whose limit this page cannot know.
  if (e?.response?.status === 413 && !detail) {
    const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
    return `File is too large to upload (${sizeMB} MB).`;
  }
  // A structured detail's sentence is already the error's message.
  return (typeof detail === 'string' && detail) || e?.message || 'Upload failed';
}

/** Where a serial upload batch is: which file is going up, and how far. */
export interface UploadBatchStatus {
  /** 1-based index of the file currently uploading. */
  current: number;
  total: number;
  fileName: string;
  percent: number;
}

/** Upload + drag-and-drop state for FilePanel (authenticated mode only —
 * upload has no ApiAdapter override; readOnly panels hide the affordances). */
export function useFileUpload({ workspaceId, onRefreshFiles }: {
  workspaceId: string;
  onRefreshFiles?: () => void;
}) {
  const { t } = useTranslation();
  // Upload state: batch status while a file is going up, then `uploadSaving`
  // while the workspace takes the file.
  const [uploadStatus, setUploadStatus] = useState<UploadBatchStatus | null>(null);
  const [uploadSaving, setUploadSaving] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // One upload at a time: a second would share the one progress bar.
  const uploadRef = useRef<AbortController | null>(null);

  // Drag-and-drop state
  const [isDragOver, setIsDragOver] = useState(false);
  const dragCounterRef = useRef(0);

  // DALTON-HOOK(multi-file-upload): one serial queue serves picks and drops
  // alike — files arriving mid-run join the back of the line instead of
  // racing a second loop — plus a run guard the effect below bumps on
  // workspace change and unmount, so a run that outlives its panel writes
  // neither state nor a list refresh for the panel that replaced it.
  const queueRef = useRef<globalThis.File[]>([]);
  // Which generation owns the processing loop (-1 = none), not a boolean: a
  // workspace switch leaves the old loop lingering until its in-flight
  // request settles, and a plain flag would make files enqueued for the new
  // workspace wait on a loop that will never drain them.
  const processingGenRef = useRef(-1);
  const runGuardRef = useRef(0);
  // Files finished by the current loop (uploaded + failed), kept in a ref so
  // an enqueue mid-run can recompute the announced total without the loop's
  // locals.
  const doneCountRef = useRef(0);

  // Cleanup runs on workspace change (before the new workspace's effect) and
  // on unmount: everything not started is dropped, and the guard strands the
  // in-flight upload — it finishes on the wire, its late result reaches no
  // state, and only the run's own captured refresh may fire for it (see
  // handleUpload). The visible progress/error also reset here so the next
  // workspace's panel never shows the old run's numbers.
  useEffect(() => () => {
    runGuardRef.current += 1;
    queueRef.current = [];
    uploadRef.current = null;
    setUploadStatus(null);
    setUploadSaving(false);
    setUploadError(null);
  }, [workspaceId]);

  // DALTON-HOOK(multi-file-upload): files go up one at a time so concurrent
  // uploads never contend for the backend or the sandbox; a file that fails is
  // recorded and the rest continue. Mid-run arrivals share this loop — and its
  // failures list, so the closing summary covers the whole queue — then the
  // list refreshes once for the entire batch.
  const handleUpload = useCallback(async (input: globalThis.File | globalThis.File[]) => {
    const isBatch = Array.isArray(input);
    const files = isBatch ? input : (input ? [input] : []);
    if (files.length === 0 || !workspaceId) return;
    const guard = runGuardRef.current;
    if (processingGenRef.current === guard) {
      if (!isBatch) return;
      setUploadError(null);
      queueRef.current.push(...files);
      // This generation's loop drains the queue; rewrite the announced total
      // now (done so far + the file in flight + the queue behind it) instead
      // of only when the loop reaches the newly queued files.
      setUploadStatus((s) => (s
        ? { ...s, total: doneCountRef.current + 1 + queueRef.current.length }
        : s));
      return;
    }
    setUploadError(null);
    queueRef.current.push(...files);
    processingGenRef.current = guard;
    doneCountRef.current = 0;
    const failures: { name: string; reason: string }[] = [];
    let uploaded = 0;
    let abandoned = false;
    // DALTON-HOOK(multi-file-upload): hold stale-build reload across the entire
    // serial upload batch (including files enqueued mid-run), not just one file.
    const release = holdReload();
    try {
      while (queueRef.current.length > 0) {
        const file = queueRef.current.shift()!;
        const ctrl = new AbortController();
        uploadRef.current = ctrl;
        // current counts finished files plus the one now starting; total adds
        // what is still queued, so a mid-run drop grows the announced total.
        const done = uploaded + failures.length;
        doneCountRef.current = done;
        setUploadSaving(false);
        setUploadStatus({
          current: done + 1,
          total: done + queueRef.current.length + 1,
          fileName: file.name,
          percent: 0,
        });
        let landed = false;
        try {
          await uploadToWorkspace(workspaceId, file, {
            signal: ctrl.signal,
            onProgress: (progress) => {
              if (runGuardRef.current !== guard) return;
              if (progress.phase === 'saving') {
                setUploadSaving(true);
              } else {
                // Parts the store did not get go again, and can be cancelled again.
                setUploadSaving(false);
                setUploadStatus((s) => (s ? { ...s, percent: progress.percent } : s));
              }
            },
          });
          landed = true;
        } catch (err: unknown) {
          if (ctrl.signal.aborted) {
            // DALTON-HOOK(multi-file-upload): a cancel that lands after a
            // workspace switch belongs to the stranded loop; the queue and the
            // panel are the new workspace's now.
            if (runGuardRef.current !== guard) {
              abandoned = true;
            } else {
              queueRef.current = [];
            }
            break;
          }
          console.error('[FilePanel] Upload failed:', err);
          if (runGuardRef.current === guard) {
            failures.push({ name: file.name, reason: uploadErrorMessage(err, file, t) });
          }
        } finally {
          if (uploadRef.current === ctrl) {
            uploadRef.current = null;
          }
        }
        if (runGuardRef.current !== guard) {
          abandoned = true;
          // DALTON-HOOK(multi-file-upload): the stranded run's last file may
          // still have landed in the old workspace — refresh that loop's own
          // list (the callback it captured at start, which only touches the
          // old workspace's cache), but write no state to the panel that
          // replaced it.
          if (landed) onRefreshFiles?.();
          break;
        }
        if (landed) uploaded++;
      }
    } finally {
      release();
      // Only the loop still owning the current generation may retire the
      // flag: an abandoned older loop clearing it would falsely signal idle
      // while a younger loop is still running.
      if (processingGenRef.current === guard) processingGenRef.current = -1;
      if (!abandoned) {
        setUploadStatus(null);
        setUploadSaving(false);
        if (uploaded > 0) onRefreshFiles?.();
        if (failures.length > 0) {
          if (!isBatch) {
            setUploadError(failures[0].reason);
          } else {
            setUploadError(t('filePanel.uploadFailedSummary', {
              count: failures.length,
              details: failures.map((f) => `${f.name} (${f.reason})`).join('; '),
            }));
          }
        }
      }
    }
  }, [workspaceId, onRefreshFiles, t]);

  /** Ends an upload still sending; once the workspace is taking the file it runs to the end. */
  const cancelUpload = useCallback(() => {
    queueRef.current = [];
    uploadRef.current?.abort();
  }, []);

  const handleFileInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    // DALTON-HOOK(multi-file-upload): the picker is multi-select; take every file
    const files = Array.from(e.target.files ?? []);
    if (files.length > 0) void handleUpload(files);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, [handleUpload]);

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current++;
    if (e.dataTransfer.types.includes('Files')) {
      setIsDragOver(true);
    }
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current--;
    if (dragCounterRef.current === 0) {
      setIsDragOver(false);
    }
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current = 0;
    setIsDragOver(false);
    // DALTON-HOOK(multi-file-upload): a drop can carry many files
    const files = Array.from(e.dataTransfer.files ?? []);
    if (files.length > 0) void handleUpload(files);
  }, [handleUpload]);

  // The bar in PanelNotices wants a bare percent of the current file; null
  // when nothing is uploading or while the workspace is saving the assembled file.
  const uploadProgress = uploadStatus === null || uploadSaving ? null : uploadStatus.percent;

  return {
    uploadProgress,
    uploadStatus,
    uploadSaving,
    cancelUpload,
    uploadError,
    setUploadError,
    fileInputRef,
    isDragOver,
    handleUpload,
    handleFileInputChange,
    handleDragEnter,
    handleDragLeave,
    handleDragOver,
    handleDrop,
  };
}
