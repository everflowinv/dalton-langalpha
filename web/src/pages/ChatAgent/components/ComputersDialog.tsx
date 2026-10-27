/**
 * The machines behind the workspace gallery, and the one place to add and
 * manage them.
 *
 * Deliberately a dialog rather than a page: a computer is infrastructure a
 * user visits to manage, not a place they work. Tier, name and storage all
 * belong to the machine, so they live here rather than on any one workspace's
 * menu, where changing them would silently change its siblings.
 *
 * Each machine is a card on a rail, drawn as the machine it is, and the
 * reader clicks through them one at a time; the last card adds another. A
 * card's Details takes the rail's place with the rest of that machine.
 *
 * The dialogs it opens (New computer, Change spec, Make primary, Remove) take
 * its place rather than piling on top: while one is open this panel steps
 * back, and the page stays dimmed exactly as much as with this panel alone.
 */
import { Suspense, useEffect, useId, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { AnimatePresence } from '@/lib/framer';

import { SwapCell } from '@/components/ui/crossfade';
import { ModalShell } from '@/components/ui/ModalShell';
import { Loader } from '@/components/ui/loader';
import { useHomeWorkspaceId } from '@/hooks/useHomeWorkspace';
import { lazyWithMotion } from '@/lib/lazyWithMotion';
import { DURATION } from '@/lib/motion';
import type { Computer, ComputerCreate } from '@/types/api';

import { useComputerProviders, useComputers } from '../hooks/useComputers';
import { useReleaseRefusedRemoteControl, useThisMac } from '../hooks/useRemoteControl';
import { openComputerSpec, type ComputersPanelView } from '../hooks/computerPanelStore';
import { deletedBeforeRefusal, useComputerActions } from '../hooks/useComputerActions';
import { useTierQuota } from '../hooks/useTierQuota';
import { isMoveActive, useWorkspaceMove } from '../hooks/useWorkspaceMove';
import ComputerCard from './ComputerCard';
import ComputerDetails from './ComputerDetails';
import { ComputerRail, type RailStart } from './computerRail';
import type { MachineActions } from './computerParts';
import './computerCards.css';
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
  THIS_MAC_OPTION,
} from './providerUi';
import { apiErrorObject, apiErrorReason, apiErrorStatus } from '../utils/api';
import { denialMessage } from '../utils/denialMessage';
import { entitlementErrorMessage } from '../utils/entitlementErrors';
import { folderMarkedOwner, sshLimitMessage } from '../utils/sshLimit';

type Translate = ReturnType<typeof useTranslation>['t'];

// Off the chat page's first load, since it carries the SSH setup. Preloadable,
// so the panel fetches it as it opens and New computer renders at once rather
// than suspending on its first open.
const CreateComputerDialog = lazyWithMotion(() => import('./CreateComputerDialog'));

interface ComputersDialogProps {
  /** What it is open onto, null while closed. */
  panel: ComputersPanelView | null;
  /** Which opening this is: a new one while it shows starts it afresh. */
  opening?: number;
  onClose: () => void;
  /** A dialog the host opened over this one (Change spec) is showing. */
  covered?: boolean;
  /** The workspace the page behind is showing, left for the gallery if a removal deletes it. */
  currentWorkspaceId?: string | null;
}

/**
 * Mounts the panel once per opening, so each one starts from what it was
 * opened onto: every view, confirmation and request of the last is gone.
 */
function ComputersDialog({ panel, opening = 0, ...rest }: ComputersDialogProps) {
  return <AnimatePresence>{panel && <ComputersPanel key={opening} opened={panel} {...rest} />}</AnimatePresence>;
}

/**
 * The New computer form: opened from the rail, or the opening's destination,
 * which has to wait until the plan is known to leave room for it.
 */
type CreateForm = 'closed' | 'fromRail' | 'awaitingRoom' | 'destination';

/** True while `mutation` is running for the machine `computerId`. */
function pendingFor(mutation: { isPending: boolean; variables?: { computerId: string } }, computerId: string): boolean {
  return mutation.isPending && mutation.variables?.computerId === computerId;
}

function ComputersPanel({
  opened,
  onClose,
  covered = false,
  currentWorkspaceId = null,
}: Omit<ComputersDialogProps, 'panel' | 'opening'> & { opened: ComputersPanelView }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const titleId = useId();
  const capNoteId = useId();
  const [createForm, setCreateForm] = useState<CreateForm>(opened.view === 'create' ? 'awaitingRoom' : 'closed');
  // The machine whose details have taken the rail's place.
  const [detailId, setDetailId] = useState<string | null>(opened.view === 'details' ? opened.computerId : null);
  // The details take the rail's height, so the dialog keeps its size both ways.
  const railViewRef = useRef<HTMLDivElement>(null);
  const [viewHeight, setViewHeight] = useState<number | null>(null);
  // Where the rail starts each time it mounts: the card it was opened onto,
  // or the one whose details it comes back from, with focus on its Details.
  const [railStart, setRailStart] = useState<RailStart>({
    id: opened.view === 'create' ? null : opened.computerId ?? null,
    index: 0,
    focus: opened.view === 'details',
    visit: 0,
  });
  // The machine this panel just created, brought to the middle of the rail
  // until the rail is left.
  const [arrivingId, setArrivingId] = useState<string | null>(null);
  const [removeTargetId, setRemoveTargetId] = useState<string | null>(null);
  const [primaryTargetId, setPrimaryTargetId] = useState<string | null>(null);
  const [backupOffTargetId, setBackupOffTargetId] = useState<string | null>(null);

  const { data, isLoading } = useComputers();
  const computers = data?.computers ?? [];
  // Read off the live list, so a row that leaves it (removed elsewhere, or by
  // this dialog) closes its confirmation rather than leaving a stale copy.
  const find = (id: string | null) => (id ? computers.find((c) => c.computer_id === id) ?? null : null);
  const detail = find(detailId);
  const removeTarget = find(removeTargetId);
  const primaryTarget = find(primaryTargetId);
  const backupOffTarget = find(backupOffTargetId);
  const { data: providerData, isLoading: providersLoading } = useComputerProviders({ enabled: true });
  const showProvider = rowsShowProvider(providerData?.providers, computers);
  const { data: quota, isLoading: quotaLoading } = useTierQuota({ enabled: true });
  // At the plan's computer cap only a machine the user brings can still be
  // added. With none of those open there is nothing for the form to offer,
  // so the button holds and says why rather than opening onto a dead end.
  // Waits for the providers: until then which ones are open is unknown.
  const atCap = computerCapReached(quota);
  const createHeld = atCap && !!providerData && !providerData.providers.some((p) => !isProviderLocked(p, true));
  // Opened onto the form, it waits until it is known not to be held (the list
  // stays covered meanwhile); held, the list shows instead, where the held
  // button says why.
  if (createForm === 'awaitingRoom' && !providersLoading && !quotaLoading) {
    setCreateForm(createHeld ? 'closed' : 'destination');
  }
  const createOpen = createForm === 'fromRail' || createForm === 'destination';

  const { power, renameComputerTo, turnOffAlwaysOn, setFilesBackup, create, remove, makePrimary } = useComputerActions();
  // In the desktop app, the row whose host key is this Mac's helper is this Mac.
  const thisMac = useThisMac({ enabled: true });
  useReleaseRefusedRemoteControl(thisMac.status);
  // Home moves with the primary, so a make-primary that has to move it runs
  // as Home's move: read here so it shows on its row, after a reopen too.
  const homeId = useHomeWorkspaceId() ?? makePrimary.data?.move?.workspace_id;
  const { data: homeMove } = useWorkspaceMove(homeId, { enabled: true });
  const homeMoving = isMoveActive(homeMove) ? homeMove : null;
  // A refused start relays the quota service's own sentence (denialMessage).
  const error = power.error ? denialMessage(power.error, t) : null;
  const createError = create.error
    ? (sshLimitMessage(create.error, t) ?? entitlementErrorMessage(create.error, t, create.variables?.resource_tier))
    : null;
  const removeError = remove.error ? removeErrorMessage(remove.error, t) : null;

  // A failed preload is not kept: the next opening imports it again.
  useEffect(() => {
    void CreateComputerDialog.preload().catch(() => {});
  }, []);

  const actionsFor = (computer: Computer): MachineActions => {
    const computerId = computer.computer_id;
    return {
      powerBusy: pendingFor(power, computerId),
      power: (action) => power.mutate({ computerId, action }),
      rename: (name) => renameComputerTo(computerId, name),
      changeSpec: () => openComputerSpec(computerId),
      alwaysOnBusy: pendingFor(turnOffAlwaysOn, computerId),
      turnOffAlwaysOn: () => {
        if (!turnOffAlwaysOn.isPending) turnOffAlwaysOn.mutate({ computerId });
      },
      filesBackupBusy: pendingFor(setFilesBackup, computerId),
      // On is safe to take at once; off asks first, since a folder lost after
      // it could only come back as it was then.
      setFilesBackup: (enabled) => {
        if (enabled) setFilesBackup.mutate({ computerId, enabled });
        else setBackupOffTargetId(computerId);
      },
      remove: () => {
        remove.reset();
        setRemoveTargetId(computerId);
      },
      movingHome: homeMoving?.target_computer_id === computerId,
      // One move at a time per user, and Home's is the one this button starts.
      makePrimaryHeld: !!homeMoving || makePrimary.isPending,
      makePrimary: () => {
        makePrimary.reset();
        setPrimaryTargetId(computerId);
      },
    };
  };

  // Leaving the rail ends an arrival, so coming back never draws it again.
  const openDetails = (computer: Computer) => {
    setViewHeight(shownHeight(railViewRef.current));
    setRailStart((last) => ({ id: computer.computer_id, index: 0, focus: true, visit: last.visit + 1 }));
    setArrivingId(null);
    setDetailId(computer.computer_id);
  };

  const openCreate = () => {
    create.reset();
    setCreateForm('fromRail');
  };

  const submitCreate = (body: ComputerCreate) => {
    create.mutate(body, {
      onSuccess: (row) => {
        setCreateForm('closed');
        setArrivingId(row.computer_id);
      },
    });
  };

  const confirmRemove = (removeKey: boolean, workspaceIds: string[], deleteFiles: boolean) => {
    if (!removeTarget || remove.isPending) return;
    // Out of its details as it goes, the rail comes back with the card after
    // it in the middle.
    const index = computers.indexOf(removeTarget);
    setRailStart((last) => ({ id: null, index, focus: true, visit: last.visit + 1 }));
    remove.mutate(
      {
        computerId: removeTarget.computer_id,
        name: removeTarget.name,
        removeKey,
        workspaceIds,
        deleteFiles,
        folder: removeTarget.root_dir,
        fingerprint: removeTarget.ssh?.fingerprint,
      },
      {
        onSuccess: ({ workspaces_deleted: deleted }) => {
          setRemoveTargetId(null);
          if (currentWorkspaceId && deleted.includes(currentWorkspaceId)) navigate('/chat');
        },
        onError: (err) => {
          if (currentWorkspaceId && deletedBeforeRefusal(err).includes(currentWorkspaceId)) navigate('/chat');
        },
      },
    );
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
      <ModalShell
        labelId={titleId}
        title={t('computer.computers', 'Computers')}
        subtitle={t('computer.computersDesc', 'Your workspaces are folders on these machines. Starting or stopping one affects every workspace it holds.')}
        onClose={onClose}
        width="wide"
        density="form"
        covered={covered || createForm !== 'closed' || !!removeTarget || !!primaryTarget || !!backupOffTarget}
      >
        {isLoading ? (
          <div className="flex items-center gap-2 py-6 justify-center" style={{ color: 'var(--color-text-tertiary)' }}>
            <Loader size={14} style={{ color: 'var(--color-accent-primary)' }} />
            <span className="text-sm">{t('computer.loading', 'Loading computers')}</span>
          </div>
        ) : (
          <SwapCell swapKey={detail ? `details:${detail.computer_id}` : `rail:${railStart.visit}`} as="div" enterDuration={DURATION.enter} sizeToIncoming>
            {detail ? (
              <ComputerDetails
                computer={detail}
                index={computers.indexOf(detail) + 1}
                total={computers.length}
                height={viewHeight}
                onBack={() => setDetailId(null)}
                thisMac={thisMac.of(detail)}
                showProvider={showProvider}
                actions={actionsFor(detail)}
              />
            ) : (
              <div ref={railViewRef}>
                <ComputerRail
                  computers={computers}
                  start={railStart}
                  arrivingId={arrivingId}
                  card={(computer, place) => (
                    <ComputerCard
                      computer={computer}
                      place={place}
                      thisMac={thisMac.of(computer)}
                      showProvider={showProvider}
                      actions={actionsFor(computer)}
                      onOpenDetails={() => openDetails(computer)}
                    />
                  )}
                  onCreate={openCreate}
                  ownMachines={!!providerData?.providers.some((p) => p.kind === 'ssh')}
                  held={createHeld}
                  heldNoteId={capNoteId}
                />
              </div>
            )}
          </SwapCell>
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
      {/* Each leaves with the panel, as well as on its own. */}
      <AnimatePresence propagate>
        {createOpen && (
          <Suspense fallback={null}>
            <CreateComputerDialog
              onClose={() => {
                if (create.isPending) return;
                // Opened onto the form, leaving it leaves the panel.
                if (createForm === 'destination') onClose();
                else setCreateForm('closed');
              }}
              onSubmit={submitCreate}
              initialPick={opened.view === 'create' && opened.pick === 'thisMac' && createForm === 'destination' ? THIS_MAC_OPTION : null}
              busy={create.isPending}
              error={createError}
              takeOver={folderMarkedOwner(create.error)}
            />
          </Suspense>
        )}
      </AnimatePresence>
      <AnimatePresence propagate>
        {removeTarget && (
          <RemoveComputerDialog
            computerId={removeTarget.computer_id}
            name={removeTarget.name}
            workspaceCount={removeTarget.workspace_count ?? 0}
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
      <AnimatePresence propagate>
        {backupOffTarget && (
          <FilesBackupOffDialog
            name={backupOffTarget.name}
            onClose={() => setBackupOffTargetId(null)}
            onConfirm={confirmFilesBackupOff}
          />
        )}
      </AnimatePresence>
      <AnimatePresence propagate>
        {primaryTarget && (
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
        // Only a workspace the confirmation did not name keeps it now.
        return t('computer.removeRefused.hasWorkspaces', 'A workspace not listed here is on it now. Check the list and try again.');
      case 'busy':
        return t('computer.removeRefused.busy', 'This computer is busy. Try again in a moment.');
    }
  }
  return apiErrorReason(err) || t('computer.removeFailed', "Couldn't remove this computer. Try again.");
}

export default ComputersDialog;

/**
 * How tall *el* shows in the box that scrolls it: its height, less what that
 * box had to scroll to fit it in a short window. Taken whole, a view that
 * scrolls inside itself would scroll twice.
 */
function shownHeight(el: HTMLElement | null): number | null {
  if (!el) return null;
  let box = el.parentElement;
  while (box && !/auto|scroll/.test(getComputedStyle(box).overflowY)) box = box.parentElement;
  return el.offsetHeight - (box ? box.scrollHeight - box.clientHeight : 0);
}
