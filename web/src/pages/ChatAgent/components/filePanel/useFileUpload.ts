import React, { useCallback, useEffect, useRef, useState } from 'react';
// DALTON-HOOK(multi-file-upload): i18n drives the batch progress and
// failure-summary strings shown while a multi-file upload runs.
import { useTranslation } from 'react-i18next';
import { uploadWorkspaceFile } from '../../utils/api';

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
  // Upload state
  const [uploadStatus, setUploadStatus] = useState<UploadBatchStatus | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

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
    setUploadStatus(null);
    setUploadError(null);
  }, [workspaceId]);

  // DALTON-HOOK(multi-file-upload): files go up one at a time so concurrent
  // uploads never contend for the backend or the sandbox; a file that fails is
  // recorded and the rest continue. Mid-run arrivals share this loop — and its
  // failures list, so the closing summary covers the whole queue — then the
  // list refreshes once for the entire batch.
  const handleUpload = useCallback(async (files: globalThis.File[]) => {
    if (files.length === 0 || !workspaceId) return;
    setUploadError(null);
    const guard = runGuardRef.current;
    queueRef.current.push(...files);
    if (processingGenRef.current === guard) {
      // This generation's loop drains the queue; rewrite the announced total
      // now (done so far + the file in flight + the queue behind it) instead
      // of only when the loop reaches the newly queued files.
      setUploadStatus((s) => (s
        ? { ...s, total: doneCountRef.current + 1 + queueRef.current.length }
        : s));
      return;
    }
    processingGenRef.current = guard;
    doneCountRef.current = 0;
    const failures: { name: string; reason: string }[] = [];
    let uploaded = 0;
    let abandoned = false;
    try {
      while (queueRef.current.length > 0) {
        const file = queueRef.current.shift()!;
        // current counts finished files plus the one now starting; total adds
        // what is still queued, so a mid-run drop grows the announced total.
        const done = uploaded + failures.length;
        doneCountRef.current = done;
        setUploadStatus({
          current: done + 1,
          total: done + queueRef.current.length + 1,
          fileName: file.name,
          percent: 0,
        });
        let landed = false;
        try {
          await uploadWorkspaceFile(workspaceId, file, null, (pct: number) => {
            if (runGuardRef.current !== guard) return;
            setUploadStatus((s) => (s ? { ...s, percent: pct } : s));
          });
          landed = true;
        } catch (err: unknown) {
          console.error('[FilePanel] Upload failed:', err);
          if (runGuardRef.current === guard) {
            const e = err as { response?: { status?: number; data?: { detail?: string } }; message?: string };
            let msg = e?.response?.data?.detail || e?.message || 'Upload failed';
            // The server's 413 names its own limit; one without a detail came from
            // something in front of it, whose limit this page cannot know.
            if (e?.response?.status === 413 && !e?.response?.data?.detail) {
              const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
              msg = `File is too large to upload (${sizeMB} MB).`;
            }
            failures.push({ name: file.name, reason: msg });
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
      // Only the loop still owning the current generation may retire the
      // flag: an abandoned older loop clearing it would falsely signal idle
      // while a younger loop is still running.
      if (processingGenRef.current === guard) processingGenRef.current = -1;
      if (!abandoned) {
        setUploadStatus(null);
        if (uploaded > 0) onRefreshFiles?.();
        if (failures.length > 0) {
          setUploadError(t('filePanel.uploadFailedSummary', {
            count: failures.length,
            details: failures.map((f) => `${f.name} (${f.reason})`).join('; '),
          }));
        }
      }
    }
  }, [workspaceId, onRefreshFiles, t]);

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
  // when nothing is uploading.
  const uploadProgress = uploadStatus === null ? null : uploadStatus.percent;

  return {
    uploadProgress,
    uploadStatus,
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
