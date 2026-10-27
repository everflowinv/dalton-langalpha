/**
 * The machines behind the workspace gallery, and the one place to add and
 * manage them.
 *
 * Deliberately a dialog rather than a page: a computer is infrastructure a
 * user visits to manage, not a place they work. Tier, name and storage all
 * belong to the machine, so they live here rather than on any one workspace's
 * menu, where changing them would silently change its siblings.
 *
 * The dialogs it opens (New computer, Change spec, Make primary, Remove) take
 * its place rather than piling on top: while one is open this panel steps
 * back, and the page stays dimmed exactly as much as with this panel alone.
 */
import { Suspense, useEffect, useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AnimatePresence } from '@/lib/framer';
import { Plus } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ModalShell } from '@/components/ui/ModalShell';
import { Loader } from '@/components/ui/loader';
import { useHomeWorkspaceId } from '@/hooks/useHomeWorkspace';
import { lazyWithMotion } from '@/lib/lazyWithMotion';
import type { ComputerCreate } from '@/types/api';

import { useComputerProviders, useComputers } from '../hooks/useComputers';
import { useReleaseRefusedRemoteControl, useThisMac } from '../hooks/useRemoteControl';
import { openComputerSpec } from '../hooks/computerPanelStore';
import { useComputerActions } from '../hooks/useComputerActions';
import { useTierQuota } from '../hooks/useTierQuota';
import { isMoveActive, useWorkspaceMove } from '../hooks/useWorkspaceMove';
import ComputerRow from './ComputerRow';
import FilesBackupOffDialog from './FilesBackupOffDialog';
import RemoveComputerDialog from './RemoveComputerDialog';
import MakePrimaryDialog from './MakePrimaryDialog';
import { PlanUpgradeHint } from './planUpgradeHint';
import {
  computerCapReached,
  isProviderLocked,
  providerHoldsOurKey,
  providerKeepsFilesOnRemove,
  rowsShowProvider,
} from './providerUi';
import { apiErrorObject, apiErrorReason, apiErrorStatus } from '../utils/api';
import { denialMessage } from '../utils/denialMessage';
import { entitlementErrorMessage } from '../utils/entitlementErrors';
import { sshLimitMessage } from '../utils/sshLimit';

type Translate = ReturnType<typeof useTranslation>['t'];

// Off the chat page's first load, since it carries the SSH setup. Preloadable,
// so the panel fetches it as it opens and New computer renders at once rather
// than suspending on its first open.
const CreateComputerDialog = lazyWithMotion(() => import('./CreateComputerDialog'));

interface ComputersDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Scroll to and highlight this machine's row. */
  focusComputerId?: string | null;
  /** Open the focused row's storage breakdown. */
  expandStorage?: boolean;
  /** A dialog the host opened over this one (Change spec) is showing. */
  covered?: boolean;
}

function ComputersDialog({
  open,
  onOpenChange,
  focusComputerId = null,
  expandStorage = false,
  covered = false,
}: ComputersDialogProps) {
  const { t } = useTranslation();
  const titleId = useId();
  const [createOpen, setCreateOpen] = useState(false);
  // The machine this dialog just created, scrolled to and highlighted like
  // a row the dialog was opened onto.
  const [createdId, setCreatedId] = useState<string | null>(null);
  const [removeTargetId, setRemoveTargetId] = useState<string | null>(null);
  const [primaryTargetId, setPrimaryTargetId] = useState<string | null>(null);
  const [backupOffTargetId, setBackupOffTargetId] = useState<string | null>(null);

  const { data, isLoading } = useComputers({ enabled: open });
  const computers = data?.computers ?? [];
  // Read off the live list, so a row that leaves it (removed elsewhere, or by
  // this dialog) closes its confirmation rather than leaving a stale copy.
  const removeTarget = removeTargetId ? computers.find((c) => c.computer_id === removeTargetId) ?? null : null;
  const primaryTarget = primaryTargetId ? computers.find((c) => c.computer_id === primaryTargetId) ?? null : null;
  const backupOffTarget = backupOffTargetId ? computers.find((c) => c.computer_id === backupOffTargetId) ?? null : null;
  const { data: providerData } = useComputerProviders({ enabled: open });
  const showProvider = rowsShowProvider(providerData?.providers, computers);
  const { data: quota } = useTierQuota({ enabled: open });
  // At the plan's computer cap only a machine the user brings can still be
  // added. With none of those open there is nothing for the form to offer,
  // so the button holds and says why rather than opening onto a dead end.
  // Waits for the providers: until then which ones are open is unknown.
  const atCap = computerCapReached(quota);
  const createHeld = atCap && !!providerData && !providerData.providers.some((p) => !isProviderLocked(p, true));
  const capNoteId = useId();
  const { power, renameComputerTo, turnOffAlwaysOn, setFilesBackup, create, remove, makePrimary } = useComputerActions();
  // In the desktop app, the row whose host key is this Mac's helper is this Mac.
  const thisMac = useThisMac({ enabled: open });
  useReleaseRefusedRemoteControl(thisMac.status);
  // Home moves with the primary, so a make-primary that has to move it runs
  // as Home's move: read here so it shows on its row, after a reopen too.
  const homeId = useHomeWorkspaceId() ?? makePrimary.data?.move?.workspace_id;
  const { data: homeMove } = useWorkspaceMove(homeId, { enabled: open });
  const homeMoving = isMoveActive(homeMove) ? homeMove : null;
  // A refused start relays the quota service's own sentence (denialMessage).
  const error = power.error ? denialMessage(power.error, t) : null;
  const createError = create.error
    ? (sshLimitMessage(create.error, t) ?? entitlementErrorMessage(create.error, t, create.variables?.resource_tier))
    : null;
  const removeError = remove.error ? removeErrorMessage(remove.error, t) : null;
  const focusId = createdId ?? focusComputerId;

  // A failed preload is not kept: opening the dialog imports it again.
  useEffect(() => {
    if (open) void CreateComputerDialog.preload().catch(() => {});
  }, [open]);

  const closeAndReset = () => {
    power.reset();
    create.reset();
    remove.reset();
    makePrimary.reset();
    setCreateOpen(false);
    setCreatedId(null);
    setRemoveTargetId(null);
    setPrimaryTargetId(null);
    setBackupOffTargetId(null);
    onOpenChange(false);
  };

  const openCreate = () => {
    create.reset();
    setCreateOpen(true);
  };

  const submitCreate = (body: ComputerCreate) => {
    create.mutate(body, {
      onSuccess: (row) => {
        setCreateOpen(false);
        setCreatedId(row.computer_id);
      },
    });
  };

  const openRemove = (computerId: string) => {
    remove.reset();
    setRemoveTargetId(computerId);
  };

  const confirmRemove = (removeKey: boolean) => {
    if (!removeTarget || remove.isPending) return;
    remove.mutate(
      {
        computerId: removeTarget.computer_id,
        name: removeTarget.name,
        removeKey,
        fingerprint: removeTarget.ssh?.fingerprint,
      },
      { onSuccess: () => setRemoveTargetId(null) },
    );
  };

  // On is safe to take at once; off asks first, since a folder lost after it
  // could only come back as it was then.
  const changeFilesBackup = (computerId: string, enabled: boolean) => {
    if (enabled) setFilesBackup.mutate({ computerId, enabled });
    else setBackupOffTargetId(computerId);
  };

  const confirmFilesBackupOff = () => {
    if (backupOffTarget) setFilesBackup.mutate({ computerId: backupOffTarget.computer_id, enabled: false });
    setBackupOffTargetId(null);
  };

  const confirmMakePrimary = () => {
    if (!primaryTarget || makePrimary.isPending) return;
    makePrimary.mutate(
      { computerId: primaryTarget.computer_id, name: primaryTarget.name },
      { onSuccess: () => setPrimaryTargetId(null) },
    );
  };

  return (
    <>
      <AnimatePresence>
        {open && (
          <ModalShell
            labelId={titleId}
            title={t('computer.computers', 'Computers')}
            subtitle={t('computer.computersDesc', 'Your workspaces are folders on these machines. Starting or stopping one affects every workspace it holds.')}
            onClose={closeAndReset}
            width="standard"
            density="form"
            covered={covered || createOpen || !!removeTarget || !!primaryTarget || !!backupOffTarget}
            footer={
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5"
                onClick={openCreate}
                disabled={createHeld}
                aria-describedby={createHeld ? capNoteId : undefined}
              >
                <Plus className="h-3.5 w-3.5" />
                {t('computer.newComputer', 'New computer')}
              </Button>
            }
          >
            {isLoading ? (
              <div className="flex items-center gap-2 py-6 justify-center" style={{ color: 'var(--color-text-tertiary)' }}>
                <Loader size={14} style={{ color: 'var(--color-accent-primary)' }} />
                <span className="text-sm">{t('computer.loading', 'Loading computers')}</span>
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {computers.length === 0 && (
                  <p className="text-sm py-4" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('computer.empty', 'No computers yet. Your first workspace creates one.')}
                  </p>
                )}
                {computers.map((computer) => (
                  <ComputerRow
                    // Keyed on the request too, so opening onto a row's storage
                    // remounts it expanded instead of re-syncing local state.
                    key={`${computer.computer_id}:${expandStorage && computer.computer_id === focusComputerId}`}
                    computer={computer}
                    focused={computer.computer_id === focusId}
                    thisMac={thisMac.of(computer)}
                    showProvider={showProvider}
                    initiallyExpanded={expandStorage && computer.computer_id === focusComputerId}
                    busy={power.isPending && power.variables?.computerId === computer.computer_id}
                    alwaysOnBusy={turnOffAlwaysOn.isPending && turnOffAlwaysOn.variables?.computerId === computer.computer_id}
                    onAction={(action) => power.mutate({ computerId: computer.computer_id, action })}
                    onRename={(name) => renameComputerTo(computer.computer_id, name)}
                    onChangeSpec={() => openComputerSpec(computer.computer_id)}
                    onTurnOffAlwaysOn={() => {
                      if (!turnOffAlwaysOn.isPending) turnOffAlwaysOn.mutate({ computerId: computer.computer_id });
                    }}
                    filesBackupBusy={setFilesBackup.isPending && setFilesBackup.variables?.computerId === computer.computer_id}
                    onFilesBackupChange={(enabled) => changeFilesBackup(computer.computer_id, enabled)}
                    onRemove={() => openRemove(computer.computer_id)}
                    movingHome={homeMoving?.target_computer_id === computer.computer_id}
                    // One move at a time per user, and Home's is the one this
                    // button starts.
                    makePrimaryHeld={!!homeMoving || makePrimary.isPending}
                    onMakePrimary={() => {
                      makePrimary.reset();
                      setPrimaryTargetId(computer.computer_id);
                    }}
                  />
                ))}
              </div>
            )}

            {createHeld && (
              <div className="flex flex-col gap-1">
                <p id={capNoteId} className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('computer.capReached', 'You have as many computers as your plan allows. Remove one to add another.')}
                </p>
                <PlanUpgradeHint
                  lead={t('computer.capPlanLimited', 'Need more computers than your plan includes?')}
                  testId="computers-cap-plan-cta"
                />
              </div>
            )}

            {error && (
              <p className="text-sm" role="alert" style={{ color: 'var(--color-icon-danger)' }}>
                {error}
              </p>
            )}
          </ModalShell>
        )}
      </AnimatePresence>
      <AnimatePresence>
        {open && createOpen && (
          <Suspense fallback={null}>
            <CreateComputerDialog
              computers={data?.computers}
              onClose={() => { if (!create.isPending) setCreateOpen(false); }}
              onSubmit={submitCreate}
              busy={create.isPending}
              error={createError}
            />
          </Suspense>
        )}
      </AnimatePresence>
      <AnimatePresence>
        {open && removeTarget && (
          <RemoveComputerDialog
            name={removeTarget.name}
            filesKeptIn={providerKeepsFilesOnRemove(removeTarget.kind) ? removeTarget.root_dir : null}
            // A helper keeps our key in its own folder, never in authorized_keys.
            holdsOurKey={providerHoldsOurKey(removeTarget.kind) && !removeTarget.ssh?.helper}
            turnsOffRemoteControl={thisMac.of(removeTarget) !== null}
            onClose={() => { if (!remove.isPending) setRemoveTargetId(null); }}
            onConfirm={confirmRemove}
            busy={remove.isPending}
            error={removeError}
          />
        )}
      </AnimatePresence>
      <AnimatePresence>
        {open && backupOffTarget && (
          <FilesBackupOffDialog
            name={backupOffTarget.name}
            onClose={() => setBackupOffTargetId(null)}
            onConfirm={confirmFilesBackupOff}
          />
        )}
      </AnimatePresence>
      <AnimatePresence>
        {open && primaryTarget && (
          <MakePrimaryDialog
            computerId={primaryTarget.computer_id}
            name={primaryTarget.name}
            onClose={() => { if (!makePrimary.isPending) setPrimaryTargetId(null); }}
            onConfirm={confirmMakePrimary}
            busy={makePrimary.isPending}
            error={makePrimary.error}
          />
        )}
      </AnimatePresence>
    </>
  );
}

/**
 * A refused remove, in this app's words for the refusals the route names,
 * else the server's own sentence, else a plain line.
 */
function removeErrorMessage(err: unknown, t: Translate): string {
  if (apiErrorStatus(err) === 409) {
    switch (apiErrorObject(err)?.code) {
      case 'primary':
        return t('computer.removeRefused.primary', "Your primary computer can't be removed.");
      case 'has_workspaces':
        return t('computer.removeRefused.hasWorkspaces', 'Delete the workspaces on this computer first.');
      case 'busy':
        return t('computer.removeRefused.busy', 'This computer is busy. Try again in a moment.');
    }
  }
  return apiErrorReason(err) || t('computer.removeFailed', "Couldn't remove this computer. Try again.");
}

export default ComputersDialog;
