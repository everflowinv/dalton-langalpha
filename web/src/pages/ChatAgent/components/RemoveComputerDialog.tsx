/**
 * Confirm removing a computer. Removal deletes a sandbox and its disk, but
 * leaves the files on a machine the user brings, so the dialog says which,
 * once, plainly, and the button names the act.
 *
 * Opened over the Computers dialog, which owns the request and the refusal;
 * mounted only while open.
 */
import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { CheckboxChoice } from '@/components/ui/choice';
import { ModalShell } from '@/components/ui/ModalShell';

interface RemoveComputerDialogProps {
  name: string;
  /** Where its files stay after the removal, on a machine the user brings; null when they go with it. */
  filesKeptIn: string | null;
  /** Our key sits in the machine's authorized_keys, so the user may choose to take it out. */
  holdsOurKey: boolean;
  /** The computer is this Mac, whose remote control goes off with it. */
  turnsOffRemoteControl?: boolean;
  onClose: () => void;
  /** `removeKey` is the box's state, and false where the box is not shown. */
  onConfirm: (removeKey: boolean) => void;
  /** The request is in flight. */
  busy: boolean;
  /** The last refusal, already worded for the user. */
  error: string | null;
}

function RemoveComputerDialog({
  name,
  filesKeptIn,
  holdsOurKey,
  turnsOffRemoteControl = false,
  onClose,
  onConfirm,
  busy,
  error,
}: RemoveComputerDialogProps) {
  const { t } = useTranslation();
  const titleId = useId();
  // On by default: a login left behind on a machine we no longer use is the
  // surprise, taking it out is not. Mounted only while open, so each open starts here.
  const [removeKey, setRemoveKey] = useState(true);

  const footer = (
    <div className="flex items-center justify-end gap-2">
      <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
        {t('common.cancel')}
      </Button>
      <Button type="button" variant="destructive" onClick={() => onConfirm(holdsOurKey && removeKey)} disabled={busy} aria-busy={busy}>
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
        {filesKeptIn
          ? t(
              'computer.removeBodyOwnMachine',
              'We stop using this machine and remove what we set up there for it. Your files in {{folder}} stay.',
              { folder: filesKeptIn },
            )
          : t('computer.removeBody', "Its sandbox and everything on its disk are deleted. This can't be undone.")}
      </p>
      {turnsOffRemoteControl && (
        <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
          {t('computer.remoteControl.removeTurnsOff', 'Remote control on this Mac turns off too.')}
        </p>
      )}
      {holdsOurKey && (
        <CheckboxChoice
          checked={removeKey}
          onCheckedChange={setRemoveKey}
          disabled={busy}
          className="items-center gap-2 rounded-md text-sm"
          style={{ color: 'var(--color-text-primary)' }}
        >
          <span>{t('computer.removeKey', 'Remove our key from ~/.ssh/authorized_keys')}</span>
        </CheckboxChoice>
      )}
      {error && !busy && (
        <p className="text-sm" role="alert" style={{ color: 'var(--color-icon-danger)' }}>
          {error}
        </p>
      )}
    </ModalShell>
  );
}

export default RemoveComputerDialog;
