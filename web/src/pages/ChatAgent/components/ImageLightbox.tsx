import { useEffect, type Ref } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { useBackdropDismiss, useDialogA11y } from '@/hooks/useDialogA11y';

interface ImageLightboxProps {
  src: string;
  alt?: string;
  open: boolean;
  onClose: () => void;
  /**
   * DALTON-HOOK(composer-attachment-preview): run the lightbox's own dialog
   * focus trap — focus in on open, Tab held in, focus handed back to the
   * opener on close. Only the composer's attachment preview passes it: a
   * lightbox opened inside a Radix dialog (a tool call's Markdown image, the
   * file panel) must leave focus management to the layer that owns it, so the
   * default keeps plain Esc-and-backdrop dismissal with no focus movement.
   */
  trapFocus?: boolean;
}

// DALTON-HOOK(composer-attachment-preview): one frame behind both behaviors —
// only the trap variant (the composer's preview) carries the dialog ref and
// aria-modal.
function LightboxFrame({
  src,
  alt,
  onClose,
  dialogRef,
  ariaModal,
}: Omit<ImageLightboxProps, 'open' | 'trapFocus'> & {
  dialogRef?: Ref<HTMLDivElement>;
  ariaModal?: boolean;
}) {
  const { t } = useTranslation();
  const backdrop = useBackdropDismiss<HTMLDivElement>(onClose);

  // A lightbox is a deliberately dark surface, not a dialog scrim: the close
  // button sits on white at 70% and the image is meant to be the only lit thing
  // on screen, so the ground stays black in both themes rather than following
  // the overlay token, which lightens to 45% black under the light palette.
  return createPortal(
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal={ariaModal || undefined}
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

/**
 * DALTON-HOOK(composer-attachment-preview): the composer's attachment
 * thumbnails open the lightbox this way, and a preview that leaves focus in
 * the composer would send the attachment on Enter and edit the draft behind
 * the scrim — so it mounts as a real dialog: `useDialogA11y` moves focus in,
 * traps Tab, answers Escape, and hands focus back to the opener on close.
 */
function ImageLightboxDialog(props: Omit<ImageLightboxProps, 'open' | 'trapFocus'>) {
  const dialogRef = useDialogA11y<HTMLDivElement>(props.onClose);
  return <LightboxFrame {...props} dialogRef={dialogRef} ariaModal />;
}

/** The default: no focus movement, just Escape and backdrop dismissal. */
function ImageLightboxPlain(props: Omit<ImageLightboxProps, 'open' | 'trapFocus'>) {
  const { onClose } = props;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);
  return <LightboxFrame {...props} />;
}

// DALTON-HOOK(composer-attachment-preview): the subtree mounts only while
// open, so whichever behavior is chosen arms per open, not once for a
// lightbox that sits mounted as null.
function ImageLightbox({ src, alt, open, onClose, trapFocus = false }: ImageLightboxProps) {
  if (!open) return null;
  const body = { src, alt, onClose };
  return trapFocus ? <ImageLightboxDialog {...body} /> : <ImageLightboxPlain {...body} />;
}

export default ImageLightbox;
