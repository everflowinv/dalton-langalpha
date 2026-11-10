import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useToast } from './use-toast';
import type { FileAttachment, ReadyAttachment } from './chat-input.types';
// DALTON-HOOK(chat-large-upload): keep 30/60 MiB inline budget and route larger files to multipart upload
import {
  MAX_CHAT_UPLOAD_FILE_SIZE,
  MAX_FILES,
  MAX_FILE_SIZE,
  MAX_TOTAL_ATTACHMENTS_BYTES,
  MAX_TOTAL_CHAT_UPLOAD_BYTES,
} from './chat-input.helpers'; // DALTON-HOOK(attachment-native-cap)
import {
  cancelChatUpload,
  formatUploadErrorMessage,
  uploadChatFile,
} from '@/lib/dalton/chatUploadClient';

interface TrackedUpload {
  controller: AbortController;
  uploadId?: string;
  workspaceKey: string;
  gen: number;
}

/** Attachment handling for the chat composer: validation (count and size),
 * inline dataUrl reading for small files, chunked multipart uploads up to
 * 200 MiB for large files, drag & drop, and paste capture. A `disabled`
 * composer takes no files, however they come: its drag handlers refuse
 * instead of attach, and a pick or paste that still arrives is dropped. */
export function useFileAttachments({
  disabled = false,
  workspaceId = 'home',
}: {
  disabled?: boolean;
  workspaceId?: string | null;
} = {}) {
  const [attachedFiles, setAttachedFiles] = useState<FileAttachment[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const { t } = useTranslation();
  const { toast } = useToast();

  // DALTON-HOOK(chat-large-upload): track active multipart uploads and sent IDs for lifecycle cleanup
  const effectiveWorkspace = workspaceId && workspaceId.trim() ? workspaceId.trim() : 'home';
  const uploadsRef = useRef(new Map<string, TrackedUpload>());
  const sentUploadIdsRef = useRef(new Set<string>());
  const latestFilesRef = useRef<FileAttachment[]>([]);
  const genCounterRef = useRef(0);

  useEffect(() => {
    latestFilesRef.current = attachedFiles;
  }, [attachedFiles]);

  const startServerUpload = useCallback(
    (itemId: string, file: File, targetWorkspaceKey: string) => {
      const prevRecord = uploadsRef.current.get(itemId);
      if (prevRecord) {
        prevRecord.controller.abort();
        if (prevRecord.uploadId && !sentUploadIdsRef.current.has(prevRecord.uploadId)) {
          void cancelChatUpload(prevRecord.uploadId);
        }
      }

      genCounterRef.current += 1;
      const currentGen = genCounterRef.current;
      const controller = new AbortController();
      const tracked: TrackedUpload = {
        controller,
        workspaceKey: targetWorkspaceKey,
        gen: currentGen,
      };
      uploadsRef.current.set(itemId, tracked);

      void uploadChatFile(file, {
        workspaceId: targetWorkspaceKey,
        signal: controller.signal,
        onInitiated: (uploadId) => {
          const active = uploadsRef.current.get(itemId);
          if (active && active.gen === currentGen && !controller.signal.aborted) {
            active.uploadId = uploadId;
          } else if (!sentUploadIdsRef.current.has(uploadId)) {
            void cancelChatUpload(uploadId);
          }
        },
        onProgress: (percent) => {
          const active = uploadsRef.current.get(itemId);
          if (!active || active.gen !== currentGen || controller.signal.aborted) return;
          setAttachedFiles((prev) =>
            prev.map((item) =>
              item.id === itemId && item.uploadStatus === 'uploading'
                ? { ...item, uploadProgress: percent }
                : item,
            ),
          );
        },
      })
        .then((res) => {
          const active = uploadsRef.current.get(itemId);
          if (!active || active.gen !== currentGen || controller.signal.aborted) {
            if (!sentUploadIdsRef.current.has(res.uploadId)) {
              void cancelChatUpload(res.uploadId);
            }
            return;
          }
          active.uploadId = res.uploadId;
          setAttachedFiles((prev) =>
            prev.map((item) =>
              item.id === itemId
                ? {
                    ...item,
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
          const active = uploadsRef.current.get(itemId);
          if (!active || active.gen !== currentGen || controller.signal.aborted) return;
          const message = formatUploadErrorMessage(err);
          setAttachedFiles((prev) =>
            prev.map((item) =>
              item.id === itemId
                ? {
                    ...item,
                    uploadStatus: 'error' as const,
                    errorMessage: message,
                  }
                : item,
            ),
          );
        });
    },
    [],
  );

  const handleFiles = useCallback(
    (newFilesList: FileList | File[]) => {
      if (disabled) return;
      const existing = latestFilesRef.current;
      const currentCount = existing.length;
      const currentTotalBytes = existing.reduce((sum, f) => sum + f.file.size, 0);
      const currentInlineBytes = existing.reduce(
        (sum, f) => (!f.uploadWorkspaceKey && !f.uploadId ? sum + f.file.size : sum),
        0,
      );
      const fileArray = Array.from(newFilesList);

      // DALTON-HOOK(chat-large-upload): enforce 0-byte, 200 MiB per-file, 600 MiB total, and 30/60 MiB inline split
      const planned: Array<{ file: File; useServerUpload: boolean }> = [];
      let acceptedTotalBytes = currentTotalBytes;
      let acceptedInlineBytes = currentInlineBytes;

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

        const fitsInline =
          file.size <= MAX_FILE_SIZE &&
          acceptedInlineBytes + file.size <= MAX_TOTAL_ATTACHMENTS_BYTES;

        acceptedTotalBytes += file.size;
        if (fitsInline) {
          acceptedInlineBytes += file.size;
        }
        planned.push({ file, useServerUpload: !fitsInline });
      }

      if (planned.length === 0) return;

      const newFiles: FileAttachment[] = planned.map(({ file, useServerUpload }) => {
        const isImage = file.type.startsWith('image/') || /\.(jpg|jpeg|png|gif|webp)$/i.test(file.name);
        return {
          id: Math.random().toString(36).slice(2, 11),
          file,
          type: file.type || (isImage ? 'image/png' : 'application/octet-stream'),
          preview: isImage && !useServerUpload ? URL.createObjectURL(file) : null,
          uploadStatus: useServerUpload ? ('uploading' as const) : ('pending' as const),
          uploadProgress: useServerUpload ? 0 : undefined,
          uploadWorkspaceKey: useServerUpload ? effectiveWorkspace : undefined,
          dataUrl: null,
        };
      });

      setAttachedFiles((prev) => [...prev, ...newFiles]);

      newFiles.forEach((entry) => {
        if (entry.uploadWorkspaceKey) {
          startServerUpload(entry.id, entry.file, entry.uploadWorkspaceKey);
          return;
        }
        const reader = new FileReader();
        reader.onload = () => {
          setAttachedFiles((prev) =>
            prev.map((p) =>
              p.id === entry.id
                ? { ...p, uploadStatus: 'complete' as const, dataUrl: reader.result as string }
                : p,
            ),
          );
        };
        reader.onerror = () => {
          setAttachedFiles((prev) => prev.filter((p) => p.id !== entry.id));
        };
        reader.readAsDataURL(entry.file);
      });
    },
    [disabled, effectiveWorkspace, startServerUpload, t, toast],
  );

  // Files a send took that are coming back to the composer, already read or uploaded.
  // The send revoked their previews, so inline images get a new one without re-uploading.
  const restoreFiles = useCallback(
    (files: ReadyAttachment[]) => {
      if (files.length === 0) return;
      const restored: FileAttachment[] = files.map((f) => {
        if (f.uploadId) {
          sentUploadIdsRef.current.delete(f.uploadId);
        }
        const isInlineImage = !f.uploadId && f.type.startsWith('image/');
        return {
          id: Math.random().toString(36).slice(2, 11),
          file: f.file,
          type: f.type,
          preview: isInlineImage ? URL.createObjectURL(f.file) : null,
          uploadStatus: 'complete' as const,
          uploadProgress: f.uploadId ? 100 : undefined,
          uploadId: f.uploadId,
          uploadWorkspaceKey: f.uploadId ? effectiveWorkspace : undefined,
          dataUrl: f.dataUrl,
        };
      });
      setAttachedFiles((prev) => [...prev, ...restored].slice(0, MAX_FILES));
    },
    [effectiveWorkspace],
  );

  // DALTON-HOOK(chat-large-upload): abort in-flight upload and delete unsent server state on remove
  const removeFile = useCallback((id: string) => {
    const tracked = uploadsRef.current.get(id);
    if (tracked) {
      uploadsRef.current.delete(id);
      tracked.controller.abort();
      if (tracked.uploadId && !sentUploadIdsRef.current.has(tracked.uploadId)) {
        void cancelChatUpload(tracked.uploadId);
      }
    }
    setAttachedFiles((prev) => {
      const target = prev.find((f) => f.id === id);
      if (target?.preview) URL.revokeObjectURL(target.preview);
      if (
        target?.uploadId &&
        target.uploadId !== tracked?.uploadId &&
        !sentUploadIdsRef.current.has(target.uploadId)
      ) {
        void cancelChatUpload(target.uploadId);
      }
      return prev.filter((f) => f.id !== id);
    });
  }, []);

  const retryFile = useCallback(
    (id: string) => {
      const target = latestFilesRef.current.find((f) => f.id === id);
      if (!target || target.uploadStatus !== 'error') return;
      setAttachedFiles((prev) =>
        prev.map((item) =>
          item.id === id
            ? {
                ...item,
                uploadStatus: 'uploading' as const,
                uploadProgress: 0,
                uploadId: undefined,
                uploadWorkspaceKey: effectiveWorkspace,
                errorMessage: undefined,
              }
            : item,
        ),
      );
      startServerUpload(id, target.file, effectiveWorkspace);
    },
    [effectiveWorkspace, startServerUpload],
  );

  const consumeSentFiles = useCallback(() => {
    for (const item of latestFilesRef.current) {
      if (item.uploadId) sentUploadIdsRef.current.add(item.uploadId);
      if (item.preview) URL.revokeObjectURL(item.preview);
    }
    for (const tracked of uploadsRef.current.values()) {
      if (tracked.uploadId) sentUploadIdsRef.current.add(tracked.uploadId);
    }
    uploadsRef.current.clear();
    setAttachedFiles([]);
  }, []);

  const clearFiles = useCallback(() => {
    const cancelledIds = new Set<string>();
    for (const tracked of uploadsRef.current.values()) {
      tracked.controller.abort();
      if (tracked.uploadId && !sentUploadIdsRef.current.has(tracked.uploadId)) {
        cancelledIds.add(tracked.uploadId);
      }
    }
    uploadsRef.current.clear();
    for (const item of latestFilesRef.current) {
      if (item.preview) URL.revokeObjectURL(item.preview);
      if (item.uploadId && !sentUploadIdsRef.current.has(item.uploadId)) {
        cancelledIds.add(item.uploadId);
      }
    }
    for (const uid of cancelledIds) {
      void cancelChatUpload(uid);
    }
    setAttachedFiles([]);
  }, []);

  // Re-scope unsent server uploads if the composer switches target workspace/Home before sending.
  const prevWorkspaceRef = useRef(effectiveWorkspace);
  useEffect(() => {
    if (prevWorkspaceRef.current === effectiveWorkspace) return;
    prevWorkspaceRef.current = effectiveWorkspace;
    const toRescope = latestFilesRef.current.filter(
      (f) => Boolean(f.uploadWorkspaceKey) && f.uploadWorkspaceKey !== effectiveWorkspace,
    );
    if (toRescope.length === 0) return;

    for (const item of toRescope) {
      if (item.uploadId && !sentUploadIdsRef.current.has(item.uploadId)) {
        void cancelChatUpload(item.uploadId);
      }
    }
    const ids = new Set(toRescope.map((f) => f.id));
    setAttachedFiles((prev) =>
      prev.map((item) =>
        ids.has(item.id)
          ? {
              ...item,
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
  }, [effectiveWorkspace, startServerUpload]);

  // Abort and clean up any unsent uploads when the composer unmounts.
  useEffect(() => {
    const trackedMap = uploadsRef.current;
    const sentSet = sentUploadIdsRef.current;
    return () => {
      const cleanupIds = new Set<string>();
      for (const tracked of trackedMap.values()) {
        tracked.controller.abort();
        if (tracked.uploadId && !sentSet.has(tracked.uploadId)) {
          cleanupIds.add(tracked.uploadId);
        }
      }
      trackedMap.clear();
      for (const item of latestFilesRef.current) {
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

  // DALTON-HOOK(chat-large-upload): expose retryFile, consumeSentFiles and clearFiles for composer UX
  return {
    attachedFiles,
    setAttachedFiles,
    isDragging,
    handleFiles,
    restoreFiles,
    removeFile,
    retryFile,
    consumeSentFiles,
    clearFiles,
    onDragEnter,
    onDragOver,
    onDragLeave,
    onDrop,
    handlePaste,
  };
}
