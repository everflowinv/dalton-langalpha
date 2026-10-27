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
import { Fragment, Suspense, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { AnimatePresence } from '@/lib/framer';
import { ChevronDown, Cpu, Pencil, Play, Plus, Square, Star, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/Disclosure';
import { ModalShell } from '@/components/ui/ModalShell';
import { Loader } from '@/components/ui/loader';
import { ToggleSwitch } from '@/components/ui/switch';
import { useHomeWorkspaceId } from '@/hooks/useHomeWorkspace';
import { useLocale } from '@/hooks/useLocale';
import { formatBytes } from '@/lib/format';
import { lazyWithMotion } from '@/lib/lazyWithMotion';
import { cn } from '@/lib/utils';
import type { Computer, ComputerCreate, ComputerSsh } from '@/types/api';

import { useComputerProviders, useComputerStorage, useComputers } from '../hooks/useComputers';
import { openComputerSpec } from '../hooks/computerPanelStore';
import { useComputerActions, type PowerAction } from '../hooks/useComputerActions';
import { usePrepareStage } from '../hooks/computerPreparation';
import { isMoveActive, useWorkspaceMove } from '../hooks/useWorkspaceMove';
import { ComputerStatusIndicator } from './computerStatusUi';
import { isComputerPreparing, isComputerStatusTransitional, prepareStepLabel, takesWorkspaces } from '../utils/computerStatus';
import { tierLabel } from './tierUi';
import { DiskBar, diskLevelColor, diskUsageLabel } from './computerDiskUi';
import { activeSpecChange, effectiveTier, specChangeLabel, SpecChangeProgress } from './specChangeUi';
import { COMPUTER_NAME_MAX } from './computerName';
import FilesBackupOffDialog from './FilesBackupOffDialog';
import RemoveComputerDialog from './RemoveComputerDialog';
import MakePrimaryDialog, { MakePrimaryButton } from './MakePrimaryDialog';
import {
  providerCanBePrimary,
  providerHoldsOurKey,
  providerKeepsFilesOnRemove,
  providerLabel,
  providerManagesSpec,
  rowsShowProvider,
} from './providerUi';
import { formatSshReached } from './sshTarget';
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
  const { power, renameComputerTo, turnOffAlwaysOn, setFilesBackup, create, remove, makePrimary } = useComputerActions();
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
      { computerId: removeTarget.computer_id, name: removeTarget.name, removeKey },
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
              <Button variant="ghost" size="sm" className="gap-1.5" onClick={openCreate}>
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
            holdsOurKey={providerHoldsOurKey(removeTarget.kind)}
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

interface ComputerRowProps {
  computer: Computer;
  focused: boolean;
  /** Name the provider beside the tier, when there are providers to tell apart. */
  showProvider: boolean;
  initiallyExpanded: boolean;
  busy: boolean;
  alwaysOnBusy: boolean;
  onAction: (action: PowerAction) => void;
  onRename: (name: string) => void;
  onChangeSpec: () => void;
  onTurnOffAlwaysOn: () => void;
  /** The file backup switch is being changed. */
  filesBackupBusy: boolean;
  onFilesBackupChange: (enabled: boolean) => void;
  onRemove: () => void;
  /** Home is moving here because this computer is being made primary. */
  movingHome: boolean;
  makePrimaryHeld: boolean;
  onMakePrimary: () => void;
}

function ComputerRow({
  computer,
  focused,
  showProvider,
  initiallyExpanded,
  busy,
  alwaysOnBusy,
  onAction,
  onRename,
  onChangeSpec,
  onTurnOffAlwaysOn,
  filesBackupBusy,
  onFilesBackupChange,
  onRemove,
  movingHome,
  makePrimaryHeld,
  onMakePrimary,
}: ComputerRowProps) {
  const { t } = useTranslation();
  const locale = useLocale();
  const rowRef = useRef<HTMLDivElement>(null);
  const breakdownId = useId();
  const prepareHintId = useId();
  const removeHintId = useId();
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const isRunning = computer.status === 'running';
  const isTransitional = isComputerStatusTransitional(computer.status);
  const preparing = isComputerPreparing(computer);
  const prepareStage = usePrepareStage(computer.computer_id);
  const disk = computer.disk ?? null;
  const specChange = activeSpecChange(computer);
  const managesSpec = providerManagesSpec(computer.kind);
  const facts = rowFacts(t, computer, showProvider, managesSpec);
  // The primary is kept, so it offers no Remove at all. Every other computer
  // offers it, held while the server would refuse, with the reason under it.
  const removeHeld = computer.is_primary ? null : removeBlock(computer, busy);
  const removeHint = removeHeld ? removeBlockHint(t, removeHeld, computer.workspace_count ?? 0) : null;

  useEffect(() => {
    if (focused) rowRef.current?.scrollIntoView?.({ block: 'nearest' });
  }, [focused]);

  return (
    <div
      ref={rowRef}
      className="flex flex-col gap-3 rounded-lg border p-3"
      style={{ borderColor: focused ? 'var(--color-border-elevated)' : 'var(--color-border-muted)' }}
      data-testid="computer-row"
    >
      <div className="flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <EditableName name={computer.name} onCommit={onRename} />
            {computer.is_primary && (
              <span
                className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[0.625rem] font-medium shrink-0"
                style={{ backgroundColor: 'var(--color-border-muted)', color: 'var(--color-text-secondary)' }}
                title={t('computer.primaryBadgeTitle', 'New workspaces are created on this computer')}
              >
                <Star className="h-3 w-3" />
                {t('computer.primaryBadge', 'Primary')}
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
            <ComputerStatusIndicator
              status={computer.status}
              preparing={preparing}
              detail={preparing ? prepareStepLabel(t, prepareStage) : null}
            />
            {facts.map(({ key, node }) => (
              <Fragment key={key}>
                <span aria-hidden="true">·</span>
                {node}
              </Fragment>
            ))}
          </div>
          {preparing && (
            <p id={prepareHintId} className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              {t(
                'computer.prepare.hint',
                "Setting up this computer can take a few minutes. You can create workspaces on it once it's ready.",
              )}
            </p>
          )}
        </div>
        {isRunning ? (
          <Button variant="ghost" size="sm" className="gap-1.5" disabled={busy || !!specChange} onClick={() => onAction('stop')} data-computer-power>
            <Square className="h-3.5 w-3.5" />
            {t('computer.stop', 'Stop')}
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5"
            disabled={busy || isTransitional || !!specChange || computer.status === 'deleted'}
            onClick={() => onAction('start')}
            data-computer-power
          >
            <Play className="h-3.5 w-3.5" />
            {t('computer.start', 'Start')}
          </Button>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          <span style={disk && disk.level !== 'healthy' ? { color: diskLevelColor(disk.level) } : undefined}>
            {disk ? diskUsageLabel(t, disk, locale) : t('computer.disk.unknown', 'Storage not measured yet')}
          </span>
          <button
            type="button"
            className="-my-1.5 inline-flex min-h-7 items-center gap-1 rounded px-1.5 hover:opacity-80"
            aria-expanded={expanded}
            aria-controls={breakdownId}
            onClick={() => setExpanded((v) => !v)}
          >
            {t('computer.disk.breakdown', 'By workspace')}
            <ChevronDown className={cn('h-3 w-3 transition-transform', expanded && 'rotate-180')} />
          </button>
        </div>
        {disk && <DiskBar disk={disk} />}
        <Disclosure open={expanded} id={breakdownId}>
          <StorageBreakdown computer={computer} enabled={expanded} />
        </Disclosure>
      </div>

      <div className="flex flex-col gap-0.5">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          {/* Always-on is no longer offered, so the switch is only there to turn it off. */}
          {managesSpec && computer.is_always_on && (
            <label className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              <ToggleSwitch
                checked
                onChange={onTurnOffAlwaysOn}
                // Mid start or stop the switch would promise a state the machine
                // is about to leave; it opens again once the machine settles.
                disabled={alwaysOnBusy || isTransitional || computer.status === 'deleted'}
                ariaLabel={t('computer.alwaysOn', 'Always-on')}
              />
              <span>{t('computer.alwaysOn', 'Always-on')}</span>
            </label>
          )}
          {/* Only a machine the user brings offers the choice: a hosted one always backs up. */}
          {computer.kind === 'ssh' && (
            <label className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              <ToggleSwitch
                checked={computer.files_backup !== false}
                onChange={() => onFilesBackupChange(computer.files_backup === false)}
                disabled={filesBackupBusy || computer.status === 'deleted'}
                ariaLabel={t('computer.filesBackup.label', 'Back up files')}
              />
              <span>{t('computer.filesBackup.label', 'Back up files')}</span>
            </label>
          )}
          {/* Wraps under the switch on a phone, still at the row's end. */}
          <div className="ml-auto flex flex-wrap items-center justify-end gap-1">
            {!managesSpec ? null : specChange ? (
              // Opens the same dialog, which shows the change's progress, not a picker.
              <button
                type="button"
                className="rounded px-1 text-xs hover:opacity-80"
                aria-label={specChangeLabel(t, specChange)}
                onClick={onChangeSpec}
              >
                <SpecChangeProgress change={specChange} asLabel />
              </button>
            ) : (
              <Button variant="ghost" size="sm" className="gap-1.5" onClick={onChangeSpec} disabled={isTransitional || computer.status === 'deleted'}>
                <Cpu className="h-3.5 w-3.5" />
                {t('workspace.changeSpec', 'Change spec')}
              </Button>
            )}
            {!computer.is_primary && providerCanBePrimary(computer.kind) && (
              <MakePrimaryButton
                movingHome={movingHome}
                disabled={makePrimaryHeld || !!specChange || !takesWorkspaces(computer)}
                onClick={onMakePrimary}
              />
            )}
            {!computer.is_primary && (
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5"
                onClick={onRemove}
                disabled={!!removeHeld}
                // A preparing row already says why, in its own hint above.
                aria-describedby={removeHeld === 'preparing' ? prepareHintId : removeHint ? removeHintId : undefined}
              >
                <Trash2 className="h-3.5 w-3.5" />
                {t('computer.remove', 'Remove')}
              </Button>
            )}
          </div>
        </div>
        {removeHint && (
          <p id={removeHintId} className="text-right text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {removeHint}
          </p>
        )}
      </div>
    </div>
  );
}

interface RowFact {
  key: string;
  node: ReactNode;
}

/**
 * What a row says after its status, each fact set off by a dot. A machine the
 * deployment sizes is told apart by its tier; one the user brings has none,
 * and is told apart by where it is reached instead.
 */
function rowFacts(t: Translate, computer: Computer, showProvider: boolean, managesSpec: boolean): RowFact[] {
  const facts: RowFact[] = [];
  if (showProvider) facts.push({ key: 'provider', node: <span>{providerLabel(computer.kind, t)}</span> });
  if (managesSpec) facts.push({ key: 'tier', node: <span>{tierLabel(t, effectiveTier(computer))}</span> });
  else if (computer.ssh) facts.push(...sshFacts(t, computer.ssh));
  if (typeof computer.workspace_count === 'number') {
    facts.push({ key: 'workspaces', node: <span>{t('computer.workspaceCount', { count: computer.workspace_count })}</span> });
  }
  return facts;
}

function sshFacts(t: Translate, ssh: ComputerSsh): RowFact[] {
  const address = formatSshReached(ssh, ssh.tunnel);
  const facts: RowFact[] = [
    { key: 'address', node: <span className="min-w-0 truncate font-mono" title={address}>{address}</span> },
  ];
  if (ssh.tunnel) {
    facts.push({ key: 'tunnel', node: <span>{t('computer.ssh.tunnelBadge', 'tunnel')}</span> });
    if (ssh.tunnel_connected === false) {
      facts.push({ key: 'offline', node: <span>{t('computer.ssh.tunnelOffline', 'tunnel offline')}</span> });
    }
  }
  return facts;
}

/**
 * Why Remove is held, in the order the user meets them: workspaces first,
 * since moving them is on the user and outlasts any start, stop or spec
 * change. Null when the server would take the request.
 */
type RemoveBlock = 'workspaces' | 'preparing' | 'specChange' | 'starting' | 'stopping' | 'busy';

function removeBlock(computer: Computer, busy: boolean): RemoveBlock | null {
  if ((computer.workspace_count ?? 0) > 0) return 'workspaces';
  if (isComputerPreparing(computer)) return 'preparing';
  if (activeSpecChange(computer)) return 'specChange';
  if (computer.status === 'creating' || computer.status === 'starting') return 'starting';
  if (computer.status === 'stopping') return 'stopping';
  // A power request in flight lands as 'starting' or 'stopping' a moment
  // later, and a removed row is about to leave the list: neither needs words.
  if (busy || computer.status === 'deleted') return 'busy';
  return null;
}

/** The line under a held Remove. A preparing row has its own hint already. */
function removeBlockHint(t: Translate, block: RemoveBlock, workspaceCount: number): string | null {
  switch (block) {
    case 'workspaces':
      return t('computer.removeBlocked.workspaces', { count: workspaceCount });
    case 'specChange':
      return t('computer.removeBlocked.specChange', 'You can remove it once the spec change finishes.');
    case 'starting':
      return t('computer.removeBlocked.starting', 'You can remove it once it has started.');
    case 'stopping':
      return t('computer.removeBlocked.stopping', 'You can remove it once it has stopped.');
    default:
      return null;
  }
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

/** The name, editable in place. Enter or blur saves; Escape backs out. */
function EditableName({ name, onCommit }: { name: string; onCommit: (name: string) => void }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const commit = () => {
    const next = draft.trim();
    setEditing(false);
    if (next && next !== name) onCommit(next);
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={draft}
        maxLength={COMPUTER_NAME_MAX}
        aria-label={t('computer.renameLabel', 'Computer name')}
        className="min-w-0 flex-1 rounded border bg-transparent px-1.5 py-0.5 text-sm font-medium"
        style={{ borderColor: 'var(--color-border-default)', color: 'var(--color-text-primary)', backgroundColor: 'var(--color-bg-input)' }}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            commit();
          } else if (e.key === 'Escape') {
            // Claimed here so the dialog around it stays open.
            e.preventDefault();
            setDraft(name);
            setEditing(false);
          }
        }}
      />
    );
  }
  return (
    <span className="group/name flex items-center gap-1 min-w-0">
      <span className="font-medium truncate" style={{ color: 'var(--color-text-primary)' }}>
        {name}
      </span>
      <button
        type="button"
        className="-m-1 shrink-0 rounded p-2 opacity-60 hover:opacity-100 focus-visible:opacity-100"
        style={{ color: 'var(--color-text-tertiary)' }}
        aria-label={t('computer.rename', 'Rename computer')}
        onClick={() => {
          setDraft(name);
          setEditing(true);
        }}
      >
        <Pencil className="h-3 w-3" />
      </button>
    </span>
  );
}

function StorageBreakdown({ computer, enabled }: { computer: Computer; enabled: boolean }) {
  const { t } = useTranslation();
  const locale = useLocale();
  const { data, isLoading, isError } = useComputerStorage(computer.computer_id, { enabled });

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        <Loader size={12} className="text-(--color-accent-primary)" />
        {t('computer.disk.measuring', 'Measuring')}
      </div>
    );
  }
  if (isError || !data) {
    return (
      <p className="py-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        {t('computer.disk.breakdownFailed', 'Could not read storage right now.')}
      </p>
    );
  }
  if (!data.live) {
    // A running machine answers stored readings only when it cannot measure:
    // a failed read if it ever reported a disk, a machine that never measures
    // (no storage quota) if not. Neither is fixed by starting it.
    return (
      <p className="py-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        {computer.status !== 'running'
          ? t('computer.disk.startForBreakdown', 'Start the computer to see what each workspace uses.')
          : data.disk
            ? t('computer.disk.breakdownFailed', 'Could not read storage right now.')
            : t('computer.disk.breakdownUnavailable', "This computer doesn't report storage by workspace.")}
      </p>
    );
  }
  const rows = [...data.workspaces].sort((a, b) => b.bytes - a.bytes);
  return (
    <ul className="flex flex-col gap-1 pt-1 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
      {rows.map((w) => (
        <li key={w.workspace_id} className="flex items-center justify-between gap-3">
          <span className="truncate">{w.name}</span>
          <span className="shrink-0 tabular-nums" style={{ color: 'var(--color-text-tertiary)' }}>{formatBytes(w.bytes, locale)}</span>
        </li>
      ))}
      {data.other_bytes > 0 && (
        <li className="flex items-center justify-between gap-3">
          <span className="truncate" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('computer.disk.other', 'System, caches and shared files')}
          </span>
          <span className="shrink-0 tabular-nums" style={{ color: 'var(--color-text-tertiary)' }}>{formatBytes(data.other_bytes, locale)}</span>
        </li>
      )}
    </ul>
  );
}

export default ComputersDialog;
