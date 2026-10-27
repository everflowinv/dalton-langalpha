/**
 * What a machine's card on the rail and its details both show: the drawing
 * in its frame, the notes under its name, its spec rows, and Start or Stop.
 */
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Laptop, Star } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { Computer } from '@/types/api';

import type { ThisMac } from '../hooks/useRemoteControl';
import type { PowerAction } from '../hooks/useComputerActions';
import { usePrepareStage } from '../hooks/computerPreparation';
import { isComputerPreparing, isComputerStatusTransitional, prepareStepLabel } from '../utils/computerStatus';
import { ComputerStatusIndicator } from './computerStatusUi';
import { tierLabel } from './tierUi';
import { activeSpecChange, effectiveTier, specChangeLabel, SpecChangeProgress } from './specChangeUi';
import { Pill } from './pill';
import { providerLabel, providerManagesSpec } from './providerUi';
import { ArtFrame } from './machineArt/ArtFrame';
import { MachineArt } from './machineArt/MachineArt';
import { machineArtOf, machineModelOf, machineStateOf } from './machineArt/machine';
import { railOrdinal } from './computerRailState';

/**
 * Everything a card or the details can ask of one machine, already bound to
 * it. The dialog owns every request; these only say which.
 */
export interface MachineActions {
  /** A start or stop of this machine is in flight. */
  powerBusy: boolean;
  power: (action: PowerAction) => void;
  rename: (name: string) => void;
  changeSpec: () => void;
  alwaysOnBusy: boolean;
  turnOffAlwaysOn: () => void;
  /** The file backup switch is being changed. */
  filesBackupBusy: boolean;
  setFilesBackup: (enabled: boolean) => void;
  remove: () => void;
  /** Home is moving here because this computer is being made primary. */
  movingHome: boolean;
  makePrimaryHeld: boolean;
  makePrimary: () => void;
}

interface MachineFrameProps {
  computer: Computer;
  index: number;
  thisMac: ThisMac | null;
  inert?: boolean;
  /** Makes the frame open the machine's details; the Details button is its keyboard way. */
  onOpen?: () => void;
}

/** The drawing in its framed ground, with mono labels in the corners. */
export function MachineFrame({ computer, index, thisMac, inert, onOpen }: MachineFrameProps) {
  const { t } = useTranslation();
  const preparing = isComputerPreparing(computer);
  const prepareStage = usePrepareStage(computer.computer_id);
  // The corner names the machine: its model, or the provider a hosted one runs on.
  const machineName =
    machineModelOf(computer) ??
    (computer.kind === 'ssh' ? t('computer.card.ownMachine', 'Own machine') : providerLabel(computer.kind, t));
  return (
    <ArtFrame
      className={cn(onOpen && 'is-openable')}
      inert={inert}
      onClick={onOpen}
      tl={railOrdinal(index)}
      tr={machineName}
      bl={
        <ComputerStatusIndicator
          status={computer.status}
          preparing={preparing}
          detail={preparing ? prepareStepLabel(t, prepareStage, computer.kind) : null}
        />
      }
      br={
        (computer.is_primary || thisMac) && (
          <>
            {computer.is_primary && (
              <Pill icon={Star} title={t('computer.primaryBadgeTitle', 'New workspaces are created on this computer')}>
                {t('computer.primaryBadge', 'Primary')}
              </Pill>
            )}
            {thisMac && <Pill icon={Laptop}>{t('computer.remoteControl.thisMacBadge', 'This Mac')}</Pill>}
          </>
        )
      }
    >
      <MachineArt
        art={machineArtOf(computer)}
        state={machineStateOf(computer, preparing)}
        brand={providerLabel(computer.kind, t)}
        tier={effectiveTier(computer)}
        label={machineName}
      />
    </ArtFrame>
  );
}

interface ComputerNotesProps {
  computer: Computer;
  thisMac: ThisMac | null;
  /** Ids the preparing note, for a control it explains. */
  prepareHintId?: string;
  /** Says what being primary means, where the badge alone is not enough. */
  primaryNote?: boolean;
}

/** The lines under a machine's name that say why it is not doing more. */
export function ComputerNotes({ computer, thisMac, prepareHintId, primaryNote = false }: ComputerNotesProps) {
  const { t } = useTranslation();
  return (
    <>
      {thisMac?.paused && (
        <p className="computer-card-note">
          {t('computer.remoteControl.paused', 'Remote control is paused. Resume it from the LangAlpha menu.')}
        </p>
      )}
      {isComputerPreparing(computer) && (
        <p id={prepareHintId} className="computer-card-note">
          {t(
            'computer.prepare.hint',
            "Setting up this computer can take a few minutes. You can create workspaces on it once it's ready.",
          )}
        </p>
      )}
      {primaryNote && computer.is_primary && (
        <p className="computer-card-note">{t('computer.primaryBadgeTitle', 'New workspaces are created on this computer')}</p>
      )}
    </>
  );
}

interface ComputerSpecRowsProps {
  computer: Computer;
  /** Offers the spec dialog beside the spec; left out, the spec is only read. */
  onChangeSpec?: () => void;
  /** False where the spec is shown among the machine's facts instead. */
  spec?: boolean;
  /** Rows after these. */
  children?: ReactNode;
}

/** The spec and the workspace count, as label and value rows. */
export function ComputerSpecRows({ computer, onChangeSpec, spec = true, children }: ComputerSpecRowsProps) {
  const { t } = useTranslation();
  return (
    <dl className="computer-card-spec">
      {spec && <SpecRow computer={computer} onChangeSpec={onChangeSpec} />}
      {typeof computer.workspace_count === 'number' && (
        <div>
          <dt>{t('computer.card.workspaces', 'Workspaces')}</dt>
          <dd>{computer.workspace_count}</dd>
        </div>
      )}
      {children}
    </dl>
  );
}

/**
 * The spec a hosted machine is sized by, as a row of a `computer-card-spec`
 * list, with the way to change it when `onChangeSpec` is given; nothing for
 * a machine whose size is its own.
 */
export function SpecRow({ computer, onChangeSpec }: Pick<ComputerSpecRowsProps, 'computer' | 'onChangeSpec'>) {
  const { t } = useTranslation();
  const specChange = activeSpecChange(computer);
  return (
    <>
      {providerManagesSpec(computer.kind) && (
        <div>
          <dt>{t('computer.card.spec', 'Spec')}</dt>
          <dd>
            {specChange && onChangeSpec ? (
              // Opens the same dialog, which shows the change's progress, not a picker.
              <button type="button" className="hover:opacity-80" aria-label={specChangeLabel(t, specChange)} onClick={onChangeSpec}>
                <SpecChangeProgress change={specChange} asLabel />
              </button>
            ) : specChange ? (
              <SpecChangeProgress change={specChange} asLabel />
            ) : onChangeSpec ? (
              <>
                <span>{tierLabel(t, effectiveTier(computer))}</span>
                <button
                  type="button"
                  className="computer-card-link"
                  aria-label={t('workspace.changeSpec', 'Change spec')}
                  onClick={onChangeSpec}
                  disabled={isComputerStatusTransitional(computer.status) || computer.status === 'deleted'}
                >
                  {t('computer.card.changeSpec', 'Change')}
                </button>
              </>
            ) : (
              tierLabel(t, effectiveTier(computer))
            )}
          </dd>
        </div>
      )}
    </>
  );
}

/** Start or Stop, whichever the machine is not. */
export function PowerButton({ computer, actions }: { computer: Computer; actions: MachineActions }) {
  const { t } = useTranslation();
  const specChange = activeSpecChange(computer);
  if (computer.status === 'running') {
    return (
      <Button size="sm" disabled={actions.powerBusy || !!specChange} onClick={() => actions.power('stop')} data-computer-power>
        {t('computer.stop', 'Stop')}
      </Button>
    );
  }
  return (
    <Button
      size="sm"
      disabled={
        actions.powerBusy || isComputerStatusTransitional(computer.status) || !!specChange || computer.status === 'deleted'
      }
      onClick={() => actions.power('start')}
      data-computer-power
    >
      {t('computer.start', 'Start')}
    </Button>
  );
}
