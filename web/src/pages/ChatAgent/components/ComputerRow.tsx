/**
 * One machine in the Computers dialog: its name, status and the facts that
 * tell it apart, its storage, and what can be done to it. The dialog owns
 * every request a row starts; the row only says which.
 */
import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Cpu, Laptop, Pencil, Play, Square, Star, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/Disclosure';
import { Loader } from '@/components/ui/loader';
import { ToggleSwitch } from '@/components/ui/switch';
import { useLocale } from '@/hooks/useLocale';
import { formatBytes } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { Computer, ComputerSsh } from '@/types/api';

import { useComputerStorage } from '../hooks/useComputers';
import type { ThisMac } from '../hooks/useRemoteControl';
import type { PowerAction } from '../hooks/useComputerActions';
import { usePrepareStage } from '../hooks/computerPreparation';
import { ComputerStatusIndicator } from './computerStatusUi';
import { isComputerPreparing, isComputerStatusTransitional, prepareStepLabel, takesWorkspaces } from '../utils/computerStatus';
import { tierLabel } from './tierUi';
import { DiskBar, diskLevelColor, diskUsageLabel } from './computerDiskUi';
import { activeSpecChange, effectiveTier, specChangeLabel, SpecChangeProgress } from './specChangeUi';
import { COMPUTER_NAME_MAX } from './computerName';
import { MakePrimaryButton } from './MakePrimaryDialog';
import { Pill } from './pill';
import { providerCanBePrimary, providerLabel, providerManagesSpec } from './providerUi';
import { formatSshReached } from './sshTarget';

type Translate = ReturnType<typeof useTranslation>['t'];

interface ComputerRowProps {
  computer: Computer;
  focused: boolean;
  /** Set when this row is this Mac, in the desktop app on it. */
  thisMac: ThisMac | null;
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
  thisMac,
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
              <Pill icon={Star} title={t('computer.primaryBadgeTitle', 'New workspaces are created on this computer')}>
                {t('computer.primaryBadge', 'Primary')}
              </Pill>
            )}
            {thisMac && <Pill icon={Laptop}>{t('computer.remoteControl.thisMacBadge', 'This Mac')}</Pill>}
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
          {thisMac?.paused && (
            <p className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('computer.remoteControl.paused', 'Remote control is paused. Resume it from the LangAlpha menu.')}
            </p>
          )}
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
  if (showProvider) {
    const label = computer.ssh?.helper ? t('computer.remoteControl.provider', 'Remote control') : providerLabel(computer.kind, t);
    facts.push({ key: 'provider', node: <span>{label}</span> });
  }
  if (managesSpec) facts.push({ key: 'tier', node: <span>{tierLabel(t, effectiveTier(computer))}</span> });
  else if (computer.ssh) facts.push(...(computer.ssh.helper ? helperFacts(t, computer.ssh) : sshFacts(t, computer.ssh)));
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
 * A Mac under remote control is an SSH computer only underneath: the login,
 * its host name and the tunnel are how the helper is reached, and none of them
 * is anything the user set up. What they chose is the account the agent works
 * as.
 */
function helperFacts(t: Translate, ssh: ComputerSsh): RowFact[] {
  const facts: RowFact[] = [
    {
      key: 'account',
      node: <span className="min-w-0 truncate">{t('computer.remoteControl.asAccount', 'as {{account}}', { account: ssh.username })}</span>,
    },
  ];
  if (ssh.tunnel_connected === false) {
    facts.push({ key: 'offline', node: <span>{t('computer.remoteControl.notConnected', 'Not connected')}</span> });
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

export default ComputerRow;
