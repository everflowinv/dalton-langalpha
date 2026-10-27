import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useToast } from './use-toast';
import type { FileAttachment } from './chat-input.types';
import { MAX_FILES, MAX_FILE_SIZE, MAX_TOTAL_ATTACHMENTS_BYTES } from './chat-input.helpers'; // DALTON-HOOK(attachment-native-cap)

/** Attachment handling for the chat composer: validation (count and size),
 * dataUrl reading with previews, drag & drop, and paste capture. */
export function useFileAttachments() {
  const [attachedFiles, setAttachedFiles] = useState<FileAttachment[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const { t } = useTranslation();
  const { toast } = useToast();

  const handleFiles = useCallback((newFilesList: FileList | File[]) => {
    const currentCount = attachedFiles.length;
    const currentBytes = attachedFiles.reduce((sum, f) => sum + f.file.size, 0);
    const fileArray = Array.from(newFilesList);

    const validFiles: File[] = [];
    let acceptedBytes = currentBytes;
    for (const file of fileArray) {
      if (currentCount + validFiles.length >= MAX_FILES) break;
      if (file.size > MAX_FILE_SIZE) {
        // DALTON-HOOK(attachment-native-cap): say so instead of dropping it silently
        toast({
          title: t('chat.attachmentTooLarge', { name: file.name, max: MAX_FILE_SIZE / (1024 * 1024) }),
          variant: 'destructive',
        });
        continue;
      }
      // DALTON-HOOK(attachment-native-cap): the message's whole payload is
      // capped too (see MAX_TOTAL_ATTACHMENTS_BYTES in the helpers); a file
      // that fits alone but breaks the budget is not added.
      if (acceptedBytes + file.size > MAX_TOTAL_ATTACHMENTS_BYTES) {
        toast({
          title: t('chat.attachmentsTotalTooLarge', {
            name: file.name,
            max: MAX_TOTAL_ATTACHMENTS_BYTES / (1024 * 1024),
          }),
          variant: 'destructive',
        });
        continue;
      }
      acceptedBytes += file.size;
      validFiles.push(file);
    }

    const newFiles: FileAttachment[] = validFiles.map((file) => {
      const isImage = file.type.startsWith('image/') || /\.(jpg|jpeg|png|gif|webp)$/i.test(file.name);
      return {
        id: Math.random().toString(36).substr(2, 9),
        file,
        type: file.type || (isImage ? 'image/png' : 'application/octet-stream'),
        preview: isImage ? URL.createObjectURL(file) : null,
        uploadStatus: 'pending' as const,
        dataUrl: null,
      };
    });

    if (newFiles.length === 0) return;

    setAttachedFiles((prev) => [...prev, ...newFiles]);

    newFiles.forEach((f) => {
      const reader = new FileReader();
      reader.onload = () => {
        setAttachedFiles((prev) =>
          prev.map((p) =>
            p.id === f.id ? { ...p, uploadStatus: 'complete' as const, dataUrl: reader.result as string } : p
          )
        );
      };
      reader.onerror = () => {
        setAttachedFiles((prev) => prev.filter((p) => p.id !== f.id));
      };
      reader.readAsDataURL(f.file);
    });
  }, [attachedFiles.length, t, toast]); // DALTON-HOOK(attachment-native-cap)

  const removeFile = useCallback((id: string) => {
    setAttachedFiles((prev) => {
      const file = prev.find((f) => f.id === id);
      if (file?.preview) URL.revokeObjectURL(file.preview);
      return prev.filter((f) => f.id !== id);
    });
  }, []);

  // Drag & Drop
  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);
  const onDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);
  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files) handleFiles(e.dataTransfer.files);
  }, [handleFiles]);

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

  return { attachedFiles, setAttachedFiles, isDragging, handleFiles, removeFile, onDragOver, onDragLeave, onDrop, handlePaste };
}
