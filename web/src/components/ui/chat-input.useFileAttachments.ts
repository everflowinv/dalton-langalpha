import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useToast } from './use-toast';
import {
  discardAttachment,
  RESTAGE_AFTER_MS,
  RESTAGE_RETRY_MS,
  SERVER_COPY_MAX_BYTES,
  stageAttachment,
  STAGED_FOR_MS,
} from '@/pages/ChatAgent/utils/attachmentUpload';
import { checkAttachment } from '@/pages/ChatAgent/utils/api/attachments';
import { apiErrorStatus } from '@/api/errors';
import { isImageFile } from '@/pages/ChatAgent/utils/fileUpload';
import { classifyUploadError } from '@/pages/ChatAgent/utils/partUpload';
import {
  cancelChatUpload,
  formatUploadErrorMessage,
  uploadChatFile,
} from '@/lib/dalton/chatUploadClient';
import type { FileAttachment, ReadyAttachment, TileUpload } from './chat-input.types';
// DALTON-HOOK(chat-large-upload): keep 30/60 MiB staged budget and route larger files to multipart upload
import {
  MAX_CHAT_UPLOAD_FILE_SIZE,
  MAX_FILES,
  MAX_FILE_SIZE,
  MAX_TOTAL_ATTACHMENTS_BYTES,
  MAX_TOTAL_CHAT_UPLOAD_BYTES,
} from './chat-input.helpers'; // DALTON-HOOK(attachment-native-cap)

const newId = () => Math.random().toString(36).slice(2, 11);

// A tile still waiting stages its file again before the server lets it go, unseen.
export { RESTAGE_AFTER_MS };

interface TrackedUpload {
  controller: AbortController;
  uploadId?: string;
  workspaceKey: string;
  gen: number;
}

/** Attachment handling for the chat composer: each small file (<= 30 MiB and
 * within 60 MiB message budget) stages on add and a send names it by id;
 * larger files (up to 200 MiB / 600 MiB total) use Dalton chunked multipart
 * upload (`chat_upload`). Also drag & drop and paste capture. A `disabled`
 * composer takes no files, however they come. */
export function useFileAttachments({
  disabled = false,
  workspaceIdRef,
  workspaceId = null,
  largeUploadWorkspaceId = null,
}: {
  disabled?: boolean;
  /** Where the message is going as it stands when a file starts uploading. */
  workspaceIdRef?: { readonly current: string | null };
  /** The same, for a render: a large file is checked again when it changes. */
  workspaceId?: string | null;
  /** Target workspace key for Dalton chunked uploads (`home` or workspace UUID). */
  largeUploadWorkspaceId?: string | null;
} = {}) {
  const [attachedFiles, setAttachedFiles] = useState<FileAttachment[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const { t } = useTranslation();
  const { toast } = useToast();

  // The list as it stands, for the uploads that settle between renders.
  const filesRef = useRef<FileAttachment[]>([]);
  const commit = useCallback((next: (prev: FileAttachment[]) => FileAttachment[]) => {
    filesRef.current = next(filesRef.current);
    setAttachedFiles(filesRef.current);
  }, []);
  const setUpload = useCallback((id: string, upload: TileUpload) => {
    commit((prev) => prev.map((f) => (f.id === id ? { ...f, upload } : f)));
  }, [commit]);

  // DALTON-HOOK(chat-large-upload): track active multipart uploads and sent IDs for lifecycle cleanup
  const effectiveWorkspace =
    largeUploadWorkspaceId && largeUploadWorkspaceId.trim()
      ? largeUploadWorkspaceId.trim()
      : workspaceId && workspaceId.trim()
        ? workspaceId.trim()
        : 'home';
  const largeUploadsRef = useRef(new Map<string, TrackedUpload>());
  const sentUploadIdsRef = useRef(new Set<string>());
  const genCounterRef = useRef(0);

  // Each staged tile's upload under way, by tile id. Aborting one ends it and
  // discards what it sent.
  const uploads = useRef(new Map<string, AbortController>());

  // The workspace each staged file was checked for, by attachment id: the
  // one its upload began with, then each it was checked against since.
  const checkedFor = useRef(new Map<string, string | null>());

  // Each ready staged tile's next staging, by tile id, and when its file became
  // ready, which the server's expiry counts from.
  const restages = useRef(new Map<string, { timer: ReturnType<typeof setTimeout>; readyAt: number }>());
  const cancelRestage = useCallback((id: string) => {
    const held = restages.current.get(id);
    if (held) clearTimeout(held.timer);
    restages.current.delete(id);
  }, []);

  /** Stages a ready tile's file again before the server lets it go, then
   *  forgets the old copy; the tile shows ready throughout. */
  const keepStaged = useCallback(function keep(id: string, readyAt: number, wait = readyAt + RESTAGE_AFTER_MS - Date.now()) {
    const prior = restages.current.get(id);
    if (prior) clearTimeout(prior.timer);
    const timer = setTimeout(async () => {
      const tile = filesRef.current.find((f) => f.id === id);
      // DALTON-HOOK(chat-large-upload): chunked large uploads stay valid until turn send without staging timers
      if (tile?.upload?.status !== 'ready' || tile.uploadId) return;
      const old = tile.upload.attachmentId;
      const ctrl = new AbortController();
      uploads.current.set(id, ctrl);
      const target = workspaceIdRef?.current ?? null;
      try {
        const fresh = await stageAttachment(tile.file, { signal: ctrl.signal, workspaceId: target });
        checkedFor.current.set(fresh, target);
        const now = filesRef.current.find((f) => f.id === id);
        // Sent or removed while it went: the copy it made is not needed.
        if (now?.upload?.status !== 'ready' || now.upload.attachmentId !== old) {
          discardAttachment(fresh);
          return;
        }
        setUpload(id, { status: 'ready', attachmentId: fresh });
        discardAttachment(old);
        keep(id, Date.now());
      } catch (err) {
        if (ctrl.signal.aborted) return;
        if (Date.now() + RESTAGE_RETRY_MS < readyAt + STAGED_FOR_MS) {
          keep(id, readyAt, RESTAGE_RETRY_MS);
        } else {
          // Out of time: the file shows failed, for the person to retry or remove.
          restages.current.delete(id);
          discardAttachment(old);
          setUpload(id, { status: 'failed', failure: classifyUploadError(err) });
        }
      } finally {
        if (uploads.current.get(id) === ctrl) uploads.current.delete(id);
      }
    }, Math.max(0, wait));
    restages.current.set(id, { timer, readyAt });
  }, [setUpload, workspaceIdRef]);

  const upload = useCallback(async (id: string, file: File) => {
    const ctrl = new AbortController();
    uploads.current.set(id, ctrl);
    const target = workspaceIdRef?.current ?? null;
    try {
      const attachmentId = await stageAttachment(file, {
        signal: ctrl.signal,
        workspaceId: target,
        onProgress: (p) => setUpload(id, { status: 'uploading', percent: p.phase === 'saving' ? null : p.percent }),
      });
      checkedFor.current.set(attachmentId, target);
      setUpload(id, { status: 'ready', attachmentId });
      keepStaged(id, Date.now());
    } catch (err) {
      if (!ctrl.signal.aborted) setUpload(id, { status: 'failed', failure: classifyUploadError(err) });
    } finally {
      uploads.current.delete(id);
    }
  }, [setUpload, keepStaged, workspaceIdRef]);

  const startServerUpload = useCallback(
    (itemId: string, file: File, targetWorkspaceKey: string) => {
      const prevRecord = largeUploadsRef.current.get(itemId);
      if (prevRecord) {
        prevRecord.controller.abort();
        if (prevRecord.uploadId && !sentUploadIdsRef.current.has(prevRecord.uploadId)) {
          void cancelChatUpload(prevRecord.uploadId);
        }
      }

      genCounterRef.current += 1;
      const currentGen = genCounterRef.current;
      const controller = new AbortController();
      // DALTON-HOOK(chat-large-upload): register generation-scoped AbortController for active upload
      const tracked: TrackedUpload = {
        controller,
        workspaceKey: targetWorkspaceKey,
        gen: currentGen,
      };
      largeUploadsRef.current.set(itemId, tracked);

      void uploadChatFile(file, {
        workspaceId: targetWorkspaceKey,
        signal: controller.signal,
        onInitiated: (uploadId) => {
          const active = largeUploadsRef.current.get(itemId);
          if (active && active.gen === currentGen && !controller.signal.aborted) {
            active.uploadId = uploadId;
          } else if (!sentUploadIdsRef.current.has(uploadId)) {
            void cancelChatUpload(uploadId);
          }
        },
        onProgress: (percent) => {
          const active = largeUploadsRef.current.get(itemId);
          if (!active || active.gen !== currentGen || controller.signal.aborted) return;
          commit((prev) =>
            prev.map((item) =>
              item.id === itemId && item.upload?.status === 'uploading'
                ? {
                    ...item,
                    upload: { status: 'uploading', percent },
                    uploadProgress: percent,
                  }
                : item,
            ),
          );
        },
      })
        .then((res) => {
          const active = largeUploadsRef.current.get(itemId);
          if (!active || active.gen !== currentGen || controller.signal.aborted) {
            if (!sentUploadIdsRef.current.has(res.uploadId)) {
              void cancelChatUpload(res.uploadId);
            }
            return;
          }
          active.uploadId = res.uploadId;
          commit((prev) =>
            prev.map((item) =>
              item.id === itemId
                ? {
                    ...item,
                    upload: { status: 'ready', attachmentId: '' },
                    // DALTON-HOOK(chat-large-upload): record completed chunked uploadId and target workspace key
                    uploadStatus: 'complete' as const,
                    uploadProgress: 100,
                    uploadId: res.uploadId,
                    uploadWorkspaceKey: targetWorkspaceKey,
                    errorMessage: undefined,
                  }
                : item,
            ),
          );
        })
        .catch((err: unknown) => {
          const active = largeUploadsRef.current.get(itemId);
          if (!active || active.gen !== currentGen || controller.signal.aborted) return;
          const message = formatUploadErrorMessage(err);
          commit((prev) =>
            prev.map((item) =>
              item.id === itemId
                ? {
                    ...item,
                    upload: { status: 'failed', failure: { kind: 'refused', message } },
                    uploadStatus: 'error' as const,
                    errorMessage: message,
                  }
                : item,
            ),
          );
        });
    },
    [commit],
  );

  // Gone with the composer: what is still uploading ends and is discarded,
  // since nothing could send it, and no file is staged again.
  useEffect(() => {
    const live = uploads.current;
    const timers = restages.current;
    // DALTON-HOOK(chat-large-upload): abort and cancel unsent chunked uploads on unmount
    const trackedMap = largeUploadsRef.current;
    const sentSet = sentUploadIdsRef.current;
    return () => {
      for (const ctrl of live.values()) ctrl.abort();
      for (const { timer } of timers.values()) clearTimeout(timer);
      timers.clear();

      const cleanupIds = new Set<string>();
      for (const tracked of trackedMap.values()) {
        tracked.controller.abort();
        if (tracked.uploadId && !sentSet.has(tracked.uploadId)) {
          cleanupIds.add(tracked.uploadId);
        }
      }
      trackedMap.clear();
      for (const item of filesRef.current) {
        if (item.preview) URL.revokeObjectURL(item.preview);
        if (item.uploadId && !sentSet.has(item.uploadId)) {
          cleanupIds.add(item.uploadId);
        }
      }
      for (const uid of cleanupIds) {
        void cancelChatUpload(uid);
      }
    };
  }, []);

  const handleFiles = useCallback(
    (newFilesList: FileList | File[]) => {
      if (disabled) return;
      const existing = filesRef.current;
      const currentCount = existing.length;
      const currentTotalBytes = existing.reduce((sum, f) => sum + f.file.size, 0);
      const currentSmallBytes = existing.reduce(
        (sum, f) => (!f.uploadWorkspaceKey && !f.uploadId ? sum + f.file.size : sum),
        0,
      );
      const fileArray = Array.from(newFilesList);

      // DALTON-HOOK(chat-large-upload): enforce 0-byte, 200 MiB per-file, 600 MiB total, and 30/60 MiB staged-vs-multipart split
      const planned: Array<{ file: File; useServerUpload: boolean }> = [];
      let acceptedTotalBytes = currentTotalBytes;
      let acceptedSmallBytes = currentSmallBytes;

      for (const file of fileArray) {
        if (currentCount + planned.length >= MAX_FILES) break;
        if (file.size <= 0) {
          toast({
            title: t('chat.attachmentEmpty', { name: file.name }),
            variant: 'destructive',
          });
          continue;
        }
        if (file.size > MAX_CHAT_UPLOAD_FILE_SIZE) {
          // DALTON-HOOK(attachment-native-cap): say so instead of dropping it silently
          toast({
            title: t('chat.attachmentTooLarge', {
              name: file.name,
              max: MAX_CHAT_UPLOAD_FILE_SIZE / (1024 * 1024),
            }),
            variant: 'destructive',
          });
          continue;
        }
        // DALTON-HOOK(attachment-native-cap): cap the message's total attachment bytes
        if (acceptedTotalBytes + file.size > MAX_TOTAL_CHAT_UPLOAD_BYTES) {
          toast({
            title: t('chat.attachmentsTotalTooLarge', {
              name: file.name,
              max: MAX_TOTAL_CHAT_UPLOAD_BYTES / (1024 * 1024),
            }),
            variant: 'destructive',
          });
          continue;
        }

        const fitsSmall =
          file.size <= MAX_FILE_SIZE &&
          acceptedSmallBytes + file.size <= MAX_TOTAL_ATTACHMENTS_BYTES;

        acceptedTotalBytes += file.size;
        if (fitsSmall) {
          acceptedSmallBytes += file.size;
        }
        planned.push({ file, useServerUpload: !fitsSmall });
      }

      if (planned.length === 0) return;

      // DALTON-HOOK(chat-large-upload): initialize tile metadata for staged and chunked paths
      const tiles: Array<FileAttachment & { useServerUpload: boolean }> = planned.map(({ file, useServerUpload }) => ({
        id: newId(),
        file,
        type: file.type || (isImageFile(file) ? 'image/png' : 'application/octet-stream'),
        upload: { status: 'uploading', percent: 0 },
        ...(useServerUpload
          ? { uploadStatus: 'uploading' as const, uploadProgress: 0, uploadWorkspaceKey: effectiveWorkspace }
          : {}),
        useServerUpload,
      }));

      commit((prev) => [...prev, ...tiles.map(({ useServerUpload: _, ...tile }) => tile)]);

      for (const tile of tiles) {
        if (tile.useServerUpload && tile.uploadWorkspaceKey) {
          startServerUpload(tile.id, tile.file, tile.uploadWorkspaceKey);
        } else {
          void upload(tile.id, tile.file);
        }
      }
    },
    [disabled, effectiveWorkspace, commit, upload, startServerUpload, t, toast],
  );

  /** Sends a failed file again. */
  const retryFile = useCallback((id: string) => {
    const tile = filesRef.current.find((f) => f.id === id);
    if (tile?.upload?.status !== 'failed') return;
    // DALTON-HOOK(chat-large-upload): retry failed chunked large upload instead of small-file stage
    if (tile.uploadWorkspaceKey || tile.uploadId) {
      commit((prev) =>
        prev.map((item) =>
          item.id === id
            ? {
                ...item,
                upload: { status: 'uploading', percent: 0 },
                uploadStatus: 'uploading' as const,
                uploadProgress: 0,
                uploadId: undefined,
                uploadWorkspaceKey: effectiveWorkspace,
                errorMessage: undefined,
              }
            : item,
        ),
      );
      startServerUpload(id, tile.file, effectiveWorkspace);
      return;
    }
    setUpload(id, { status: 'uploading', percent: 0 });
    void upload(id, tile.file);
  }, [commit, setUpload, upload, startServerUpload, effectiveWorkspace]);

  /** Files a send took that come back to the composer, already on the server.
   *  What no longer fits is not coming back, so the server forgets it. */
  const restoreFiles = useCallback((files: ReadyAttachment[]) => {
    const room = Math.max(0, MAX_FILES - filesRef.current.length);
    for (const f of files.slice(room)) {
      if (f.attachmentId) discardAttachment(f.attachmentId);
      if (f.uploadId) void cancelChatUpload(f.uploadId);
    }
    const kept = files.slice(0, room);
    const restored = kept.map((f): FileAttachment => {
      // DALTON-HOOK(chat-large-upload): restore refused/draft chunked uploads without calling stageAttachment
      if (f.uploadId) {
        sentUploadIdsRef.current.delete(f.uploadId);
        return {
          id: newId(),
          file: f.file,
          type: f.type,
          upload: { status: 'ready', attachmentId: '' },
          uploadStatus: 'complete',
          uploadProgress: 100,
          uploadId: f.uploadId,
          uploadWorkspaceKey: effectiveWorkspace,
        };
      }
      return {
        id: newId(),
        file: f.file,
        type: f.type,
        upload: { status: 'ready', attachmentId: f.attachmentId },
      };
    });
    if (restored.length > 0) commit((prev) => [...prev, ...restored]);
    restored.forEach((tile, i) => {
      if (!tile.uploadId) keepStaged(tile.id, kept[i].readyAt ?? Date.now());
    });
  }, [commit, keepStaged, effectiveWorkspace]);

  // DALTON-HOOK(chat-large-upload): abort in-flight upload and delete unsent server state on remove
  const removeFile = useCallback((id: string) => {
    const tile = filesRef.current.find((f) => f.id === id);
    const tracked = largeUploadsRef.current.get(id);
    if (tracked) {
      largeUploadsRef.current.delete(id);
      tracked.controller.abort();
      if (tracked.uploadId && !sentUploadIdsRef.current.has(tracked.uploadId)) {
        void cancelChatUpload(tracked.uploadId);
      }
    }
    uploads.current.get(id)?.abort();
    cancelRestage(id);
    if (tile) {
      if (tile.preview) URL.revokeObjectURL(tile.preview);
      if (
        tile.uploadId &&
        tile.uploadId !== tracked?.uploadId &&
        !sentUploadIdsRef.current.has(tile.uploadId)
      ) {
        void cancelChatUpload(tile.uploadId);
      }
      if (!tile.uploadId && tile.upload?.status === 'ready' && tile.upload.attachmentId) {
        discardAttachment(tile.upload.attachmentId);
      }
    }
    commit((prev) => prev.filter((f) => f.id !== id));
  }, [commit, cancelRestage]);

  /** Every file, as for a draft thrown away. */
  const discardFiles = useCallback(() => {
    for (const f of [...filesRef.current]) removeFile(f.id);
  }, [removeFile]);

  const consumeSentFiles = useCallback(() => {
    // DALTON-HOOK(chat-large-upload): mark chunked uploadIds as sent so unmount/remove cleanup won't cancel them
    for (const item of filesRef.current) {
      if (item.uploadId) sentUploadIdsRef.current.add(item.uploadId);
      if (item.preview) URL.revokeObjectURL(item.preview);
    }
    for (const tracked of largeUploadsRef.current.values()) {
      if (tracked.uploadId) sentUploadIdsRef.current.add(tracked.uploadId);
    }
    largeUploadsRef.current.clear();
  }, []);

  /** A ready staged tile past the point it is staged again: a tab asleep through its
   *  timer still holds an id the server may already have let go. */
  const isStale = useCallback((f: FileAttachment) => {
    if (f.uploadId || f.uploadWorkspaceKey) return false;
    if (f.upload?.status !== 'ready') return false;
    const held = restages.current.get(f.id);
    return !!held && Date.now() - held.readyAt >= RESTAGE_AFTER_MS;
  }, []);

  /** Whether a send now would be held by a stale file (`takeFiles`). */
  const holdsStale = useCallback(() => filesRef.current.some(isStale), [isStale]);

  /**
   * The uploaded files, for a send, which takes them out of the composer. The
   * message names them, so they stay on the server, and a staging again
   * under way for one ends, its copy discarded.
   *
   * A stale file is not sent: it uploads again now, showing as uploading, and
   * the send is held (null, nothing taken) until it is ready. So is a send
   * with any file uploading or failed, read here rather than from a render,
   * which a change from a timer can be ahead of.
   */
  const takeFiles = useCallback((): ReadyAttachment[] | null => {
    const stale = filesRef.current.filter(isStale);
    if (stale.length > 0) {
      for (const f of stale) {
        if (f.upload?.status !== 'ready') continue;
        uploads.current.get(f.id)?.abort();
        cancelRestage(f.id);
        discardAttachment(f.upload.attachmentId);
        setUpload(f.id, { status: 'uploading', percent: 0 });
        void upload(f.id, f.file);
      }
      return null;
    }
    if (filesRef.current.some((f) => f.upload?.status !== 'ready')) return null;
    const taken: ReadyAttachment[] = [];
    for (const f of filesRef.current) {
      if (f.upload?.status !== 'ready') continue;
      // DALTON-HOOK(chat-large-upload): hand off chunked uploadId in ReadyAttachment without calling discardAttachment
      if (f.uploadId) {
        sentUploadIdsRef.current.add(f.uploadId);
        taken.push({
          file: f.file,
          type: f.type,
          attachmentId: '',
          uploadId: f.uploadId,
          dataUrl: null,
        });
        largeUploadsRef.current.delete(f.id);
        continue;
      }
      const readyAt = restages.current.get(f.id)?.readyAt;
      taken.push({ file: f.file, type: f.type, attachmentId: f.upload.attachmentId, ...(readyAt ? { readyAt } : {}) });
      uploads.current.get(f.id)?.abort();
      cancelRestage(f.id);
    }
    commit((prev) => prev.filter((f) => f.upload?.status !== 'ready'));
    return taken;
  }, [commit, cancelRestage, isStale, setUpload, upload]);

  // DALTON-HOOK(chat-large-upload): re-scope unsent Dalton server uploads if the composer switches target workspace/Home before sending.
  const prevLargeWorkspaceRef = useRef(effectiveWorkspace);
  useEffect(() => {
    if (prevLargeWorkspaceRef.current === effectiveWorkspace) return;
    prevLargeWorkspaceRef.current = effectiveWorkspace;
    const toRescope = filesRef.current.filter(
      (f) => Boolean(f.uploadWorkspaceKey) && f.uploadWorkspaceKey !== effectiveWorkspace,
    );
    if (toRescope.length === 0) return;

    for (const item of toRescope) {
      if (item.uploadId && !sentUploadIdsRef.current.has(item.uploadId)) {
        void cancelChatUpload(item.uploadId);
      }
    }
    const ids = new Set(toRescope.map((f) => f.id));
    commit((prev) =>
      prev.map((item) =>
        ids.has(item.id)
          ? {
              ...item,
              upload: { status: 'uploading', percent: 0 },
              uploadStatus: 'uploading' as const,
              uploadProgress: 0,
              uploadId: undefined,
              uploadWorkspaceKey: effectiveWorkspace,
              errorMessage: undefined,
            }
          : item,
      ),
    );
    for (const item of toRescope) {
      startServerUpload(item.id, item.file, effectiveWorkspace);
    }
  }, [effectiveWorkspace, commit, startServerUpload]);

  // A file larger than the server copies was checked only against where the
  // message was going when it began, if anywhere: one the new target's
  // computer cannot receive fails here, before Send, rather than in the turn.
  useEffect(() => {
    if (!workspaceId) return;
    for (const f of attachedFiles) {
      // DALTON-HOOK(chat-large-upload): skip upstream stage checkAttachment for chunked Dalton uploads
      if (f.uploadId || f.uploadWorkspaceKey) continue;
      if (f.upload?.status !== 'ready' || f.file.size <= SERVER_COPY_MAX_BYTES) continue;
      const { attachmentId } = f.upload;
      if (checkedFor.current.get(attachmentId) === workspaceId) continue;
      checkedFor.current.set(attachmentId, workspaceId);
      void checkAttachment(attachmentId, workspaceId).catch((err) => {
        // Not answered: the turn checks it again.
        if (apiErrorStatus(err) !== 413) return;
        // Checked since for another target, or no longer this tile's file.
        if (checkedFor.current.get(attachmentId) !== workspaceId) return;
        const now = filesRef.current.find((t) => t.id === f.id);
        if (now?.upload?.status !== 'ready' || now.upload.attachmentId !== attachmentId) return;
        cancelRestage(f.id);
        discardAttachment(attachmentId);
        setUpload(f.id, { status: 'failed', failure: classifyUploadError(err) });
      });
    }
  }, [workspaceId, attachedFiles, cancelRestage, setUpload]);

  const uploadingCount = attachedFiles.filter((f) => f.upload?.status === 'uploading').length;
  const failedCount = attachedFiles.filter((f) => f.upload?.status === 'failed').length;

  // Drag & Drop. A disabled composer refuses a drag over it (shown as such,
  // and kept from reaching the page, which would open the file in its place)
  // and a drop on it is not attached to a message that cannot be sent.
  //
  // The drag is over the composer while some element in it has been entered
  // and not left. Crossing from one child into another fires the child's
  // dragenter before the last one's dragleave, and both bubble here, so a
  // dragleave alone would hide the overlay at every inner edge and the next
  // dragover would show it again. An element removed under the pointer (the
  // send cell's Stop as the turn ends) is never left, so it stops counting
  // once it is out of the document.
  const dragEntered = useRef(new Set<Node>());
  const endDrag = useCallback(() => {
    dragEntered.current.clear();
    setIsDragging(false);
  }, []);
  const onDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    if (e.target instanceof Node) dragEntered.current.add(e.target);
    if (!disabled) setIsDragging(true);
  }, [disabled]);
  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    if (disabled) e.dataTransfer.dropEffect = 'none';
  }, [disabled]);
  const onDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const entered = dragEntered.current;
    if (e.target instanceof Node) entered.delete(e.target);
    for (const node of entered) if (!node.isConnected) entered.delete(node);
    if (entered.size === 0) setIsDragging(false);
  }, []);
  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    endDrag();
    if (e.dataTransfer.files) handleFiles(e.dataTransfer.files);
  }, [endDrag, handleFiles]);

  // A drag that ends without leaving the composer the way it came in (dropped
  // elsewhere on the page, or cancelled) still ends here.
  useEffect(() => {
    if (!isDragging) return;
    window.addEventListener('dragend', endDrag);
    window.addEventListener('drop', endDrag);
    return () => {
      window.removeEventListener('dragend', endDrag);
      window.removeEventListener('drop', endDrag);
    };
  }, [isDragging, endDrag]);

  // Paste Handling
  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData.items;
    const pastedFiles = [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].kind === 'file') {
        const file = items[i].getAsFile();
        if (file) pastedFiles.push(file);
      }
    }
    if (pastedFiles.length > 0) {
      e.preventDefault();
      handleFiles(pastedFiles);
    }
  }, [handleFiles]);

  // DALTON-HOOK(chat-large-upload): expose retryFile, consumeSentFiles and clearFiles alongside upstream controls
  return {
    attachedFiles,
    uploadingCount,
    failedCount,
    isDragging,
    handleFiles,
    retryFile,
    restoreFiles,
    removeFile,
    discardFiles,
    takeFiles,
    holdsStale,
    consumeSentFiles,
    clearFiles: discardFiles,
    onDragEnter,
    onDragOver,
    onDragLeave,
    onDrop,
    handlePaste,
  };
}
