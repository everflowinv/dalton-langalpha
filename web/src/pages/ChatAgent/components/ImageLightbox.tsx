import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { useBackdropDismiss, useDialogA11y } from '@/hooks/useDialogA11y';

interface ImageLightboxProps {
  src: string;
  alt?: string;
  open: boolean;
  onClose: () => void;
}

/**
 * DALTON-HOOK(composer-attachment-preview): the composer's attachment
 * thumbnails open this lightbox, and a preview that leaves focus in the
 * composer would send the attachment on Enter and edit the draft behind the
 * scrim — so it became a real dialog: `useDialogA11y` moves focus in, traps
 * Tab, answers Escape, and hands focus back to the opener on close.
 */
function ImageLightboxDialog({ src, alt, onClose }: Omit<ImageLightboxProps, 'open'>) {
  const { t } = useTranslation();
  const dialogRef = useDialogA11y<HTMLDivElement>(onClose);
  const backdrop = useBackdropDismiss<HTMLDivElement>(onClose);

  // A lightbox is a deliberately dark surface, not a dialog scrim: the close
  // button sits on white at 70% and the image is meant to be the only lit thing
  // on screen, so the ground stays black in both themes rather than following
  // the overlay token, which lightens to 45% black under the light palette.
  return createPortal(
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={alt ?? t('chat.composer.previewImage')}
      className="fixed inset-0 z-1020 flex items-center justify-center bg-black/90 scrim-in"
      {...backdrop}
    >
      <button
        onClick={onClose}
        className="absolute top-4 right-4 z-10 rounded-full p-2 text-white/70 hover:text-white hover:bg-white/10 transition-colors"
        aria-label={t('common.close')}
      >
        <X className="h-6 w-6" />
      </button>
      <img
        src={src}
        alt={alt}
        className="max-w-[90vw] max-h-[90vh] object-contain rounded-lg"
        // Not for the backdrop, which checks its own target: the lightbox is a
        // portal, so a click on the image would otherwise bubble up the React
        // tree into the chat message that rendered it.
        onClick={(e) => e.stopPropagation()}
      />
    </div>,
    document.body
  );
}

// DALTON-HOOK(composer-attachment-preview): the dialog subtree mounts only
// while open, so the focus trap above arms per open, not once for a lightbox
// that sits mounted as null.
function ImageLightbox({ src, alt, open, onClose }: ImageLightboxProps) {
  if (!open) return null;
  return <ImageLightboxDialog src={src} alt={alt} onClose={onClose} />;
}

export default ImageLightbox;
