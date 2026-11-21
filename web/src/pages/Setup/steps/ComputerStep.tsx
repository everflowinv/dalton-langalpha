import { useId, useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
// DALTON-HOOK(dalton-setup): Dalton build hides Mac pairing and uses cloud by default
import { AlertTriangle, ChevronRight, Cloud, Laptop, SquareTerminal } from 'lucide-react';
import { WizardStepHeader } from '@/components/ui/wizard';
import { isDaltonBuild } from '@/config/brand';
import { useOnboardingProgress } from '@/hooks/useOnboardingProgress';
import { canAddThisMac, canRemoteControl, desktop, isDesktopShell } from '@/lib/desktop';
import { serverTakesRemoteControl, useThisMacAvailability } from '@/pages/ChatAgent/hooks/useRemoteControl';
import { useComputerProviders } from '@/pages/ChatAgent/hooks/useComputers';
import { useComputerActions } from '@/pages/ChatAgent/hooks/useComputerActions';
import { useHomePlacement } from '@/pages/ChatAgent/hooks/useHomePlacement';
import MakePrimaryDialog from '@/pages/ChatAgent/components/MakePrimaryDialog';
import { SetupFooter } from '../SetupFooter';
import { useAnswerFlow } from '../answers/flow';
import { Group } from '@/components/modelAccess/parts';
import { AccessRow } from '@/components/modelAccess/AccessRow';
import { AnswersStrip, RowButton, SetupNote, StepError, StepLoadFailed, StepLoading } from '../answers/parts';

const S = 'modelAccess.claudeCode.setup';

type MacRowKind = 'pair' | 'update' | 'continue';

/** The Mac row by why it shows: what it says, and where its button goes. */
const MAC_ROW: Record<MacRowKind, { detail: string; action?: { to: string; label: string } }> = {
  pair: { detail: 'setup.answers.computer.macDesktop', action: { to: '/setup/this-mac', label: 'setup.answers.computer.pairThisMac' } },
  update: { detail: 'setup.answers.computer.macUpdateApp' },
  continue: { detail: S + '.mac.noteBrowser', action: { to: '/setup/mac', label: 'setup.answers.computer.continueOnMac' } },
};

function macRowKind(inApp: boolean, thisMac: { offered: boolean; added: boolean | null | undefined }, takesRemoteControl: boolean): MacRowKind | null {
  if (!inApp) return takesRemoteControl ? 'continue' : null;
  if (thisMac.offered && thisMac.added === false) return 'pair';
  return !canRemoteControl() && desktop?.platform === 'darwin' ? 'update' : null;
}

/**
 * A computer of the user's own, where tasks run on one and none is connected
 * yet: their Mac through Remote control, which the Mac app pairs (from a
 * browser the user continues there; in the app it pairs this Mac), or a
 * computer over SSH, added on the next page without leaving setup. A
 * computer already connected is listed, and Continue goes on with it.
 *
 * The Mac row shows only where it can lead somewhere: in the app, where this
 * Mac can pair (the shell supports Remote control and the server takes it),
 * or a line to update an app too old to; in a browser, where the server
 * takes Remote control. Inside the app it never sends the user to
 * /setup/mac, which is the browser's page. Where Home is on a cloud computer,
 * each listed one can be made primary, moving Home there.
 */
export default function ComputerStep() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const headingId = useId();
  const flow = useAnswerFlow('computer');
  const { update, isUpdating } = useOnboardingProgress();
  const [advancedSshOpen, setAdvancedSshOpen] = useState(false);
  const [cloudSwitchFailed, setCloudSwitchFailed] = useState(false);
  const inApp = isDesktopShell();
  // DALTON-HOOK(dalton-setup): skip Mac pairing probes and hide Mac row in Dalton builds
  const availability = useThisMacAvailability({ enabled: !isDaltonBuild && canAddThisMac(), poll: false });
  const providers = useComputerProviders({ enabled: isDaltonBuild || !inApp });
  const sshOffered = !isDaltonBuild || !!providers.data?.providers.some((p) => p.kind === 'ssh');
  const kind = isDaltonBuild ? null : macRowKind(inApp, availability, serverTakesRemoteControl(providers.data));
  const mac = kind && MAC_ROW[kind];
  const macAction = mac?.action;
  const claudeCode = flow.answers?.models === 'claude_code';
  const offerHome = flow.answers?.runsOn === 'own_computer' && !!flow.facts?.homeOnCloud;
  const { makePrimary } = useComputerActions();
  const { data: placement } = useHomePlacement({ enabled: offerHome });
  const [homeTargetId, setHomeTargetId] = useState<string | null>(null);
  const homeTarget = flow.ownComputers.find((c) => c.computer_id === homeTargetId);
  // Its ways in stay out until the computers already connected are read.
  if (flow.isLoading || (isDaltonBuild && !providers.data && !providers.isError)) return <StepLoading />;
  // Whether this Mac can pair is unknown until the server answers.
  if (!isDaltonBuild && availability.settled && availability.serverFailed) return <StepLoadFailed onRetry={availability.retryServer} />;

  const continueOnCloud = () => {
    setCloudSwitchFailed(false);
    update({ runs_on: 'cloud' }).then(
      () => navigate('/setup/default', { replace: true }),
      () => setCloudSwitchFailed(true),
    );
  };

  const sshRow = (
    <AccessRow
      testId="setup-computer-ssh"
      icon={<SquareTerminal />}
      name={t(S + '.ssh.name')}
      tag={t('computer.ssh.previewLabel')}
      wrapDetail
      detail={t(S + '.ssh.note')}
      action={<RowButton onClick={() => navigate('/setup/ssh')}>{t(S + '.ssh.add')}</RowButton>}
    />
  );

  return (
    <div>
      {flow.first && flow.answers && <AnswersStrip answers={flow.answers} />}
      <WizardStepHeader
        titleId={headingId}
        title={t(isDaltonBuild && !sshOffered ? 'setup.answers.computer.cloudReadyTitle' : 'setup.answers.computer.title')}
        helper={t(
          isDaltonBuild && !sshOffered
            ? 'setup.answers.computer.cloudReadyLead'
            : claudeCode ? 'setup.answers.computer.leadClaudeCode' : 'setup.answers.computer.lead',
        )}
      />
      <div className="flex flex-col gap-5">
        {flow.ownComputers.length > 0 && (
          <Group title={t(S + '.yourComputers')}>
            {flow.ownComputers.map((c) => (
              <AccessRow
                key={c.computer_id}
                testId="setup-own-computer"
                icon={c.ssh?.helper ? <Laptop /> : <SquareTerminal />}
                name={c.name}
                detail={c.ssh ? `${c.ssh.username}@${c.ssh.host}` : undefined}
                status="connected"
                action={offerHome && <RowButton onClick={() => setHomeTargetId(c.computer_id)}>{t('computer.makePrimary.button')}</RowButton>}
              />
            ))}
          </Group>
        )}
        {isDaltonBuild ? (
          /* DALTON-HOOK(dalton-setup): managed cloud computer default; SSH tucked into Advanced when offered */
          <>
            <Group title={t('setup.answers.use.serverComputer')}>
              <AccessRow
                testId="setup-computer-cloud"
                icon={<Cloud />}
                name={t('setup.answers.computer.cloudReadyRow')}
                detail={t('setup.answers.computer.cloudReadyDetail')}
                status="connected"
              />
            </Group>
            {sshOffered && (
              <div>
                <button
                  type="button"
                  onClick={() => setAdvancedSshOpen((v) => !v)}
                  className="inline-flex items-center gap-1 text-xs font-medium"
                  style={{ color: 'var(--color-text-tertiary)' }}
                  aria-expanded={advancedSshOpen}
                >
                  <ChevronRight className={`h-3 w-3 transition-transform ${advancedSshOpen ? 'rotate-90' : ''}`} />
                  {t('setup.answers.computer.advancedSsh')}
                </button>
                {advancedSshOpen && <div className="mt-3">{sshRow}</div>}
              </div>
            )}
          </>
        ) : (
          <Group title={t(S + '.waysTitle')}>
            {mac && (
              <AccessRow
                testId="setup-computer-mac"
                icon={<Laptop />}
                name={t(S + '.mac.name')}
                tag={t('computer.ssh.previewLabel')}
                wrapDetail
                detail={t(mac.detail)}
                action={macAction && <RowButton onClick={() => navigate(macAction.to)}>{t(macAction.label)}</RowButton>}
              />
            )}
            {sshRow}
          </Group>
        )}
      </div>
      {(!isDaltonBuild || (sshOffered && advancedSshOpen)) && (
        <>
          <SetupNote icon={<AlertTriangle />}>{t('setup.answers.computer.accountNote')}</SetupNote>
          <SetupNote icon={<Laptop />}>{t('setup.answers.computer.onlineNote')}</SetupNote>
        </>
      )}
      {(flow.finishFailed || cloudSwitchFailed) && <StepError>{t('setup.answers.saveFailed')}</StepError>}
      <SetupFooter
        back={flow.back ? { label: t('setup.back'), onClick: flow.back } : null}
        primary={flow.ownComputers.length > 0 ? {
          label: t('setup.continue'),
          onClick: () => void flow.finish(),
          disabled: flow.finishing,
          busy: flow.finishing,
        } : isDaltonBuild ? {
          label: t('setup.continue'),
          onClick: continueOnCloud,
          disabled: isUpdating,
          busy: isUpdating,
        } : null}
      />
      {homeTarget && (
        <MakePrimaryDialog
          computer={homeTarget}
          placement={placement}
          onClose={() => { if (!makePrimary.isPending) setHomeTargetId(null); }}
          onConfirm={({ turnOnBackup, fromBackup }) => makePrimary.mutate(
            { computerId: homeTarget.computer_id, name: homeTarget.name, fromBackup, turnOnBackup },
            { onSuccess: () => setHomeTargetId(null) },
          )}
          busy={makePrimary.isPending}
          error={makePrimary.error}
        />
      )}
    </div>
  );
}
