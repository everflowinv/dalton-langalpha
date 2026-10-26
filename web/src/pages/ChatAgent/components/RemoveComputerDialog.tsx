/**
 * Confirm removing a computer. Removal deletes its sandbox and its disk, so
 * the dialog says that once, plainly, and the button names the act.
 *
 * Opened over the Computers dialog, which owns the request and the refusal;
 * mounted only while open.
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ModalShell } from '@/components/ui/ModalShell';

interface RemoveComputerDialogProps {
  name: string;
  onClose: () => void;
  onConfirm: () => void;
  /** The request is in flight. */
  busy: boolean;
  /** The last refusal, already worded for the user. */
  error: string | null;
}

function RemoveComputerDialog({ name, onClose, onConfirm, busy, error }: RemoveComputerDialogProps) {
  const { t } = useTranslation();
  const titleId = useId();

  const footer = (
    <div className="flex items-center justify-end gap-2">
      <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
        {t('common.cancel')}
      </Button>
      <Button type="button" variant="destructive" onClick={onConfirm} disabled={busy} aria-busy={busy}>
        {busy ? t('computer.removing', 'Removing...') : t('computer.removeConfirm', 'Remove computer')}
      </Button>
    </div>
  );

  return (
    <ModalShell
      labelId={titleId}
      title={t('computer.removeTitle', 'Remove {{name}}?', { name })}
      onClose={onClose}
      closeDisabled={busy}
      width="narrow"
      density="form"
      stacked
      footer={footer}
    >
      <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
        {t('computer.removeBody', "Its sandbox and everything on its disk are deleted. This can't be undone.")}
      </p>
      {error && !busy && (
        <p className="text-sm" role="alert" style={{ color: 'var(--color-icon-danger)' }}>
          {error}
        </p>
      )}
    </ModalShell>
  );
}

export default RemoveComputerDialog;
