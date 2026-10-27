/**
 * Create a computer: a name, where it runs, and its spec.
 *
 * Opened over the Computers dialog, which owns the request and scrolls to the
 * new row once it lands. Mounted only while open, so every opening starts
 * from a blank form. The provider and the tier are locked by plan the same
 * way, and either lock brings up the same way out as the spec dialog. At the
 * plan's computer cap only a machine the user brings is left open, and the
 * Computers dialog opens this form only when one is. In the desktop app on a
 * Mac, this Mac itself leads the choices, added by remote control as an SSH
 * computer.
 */
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ModalShell } from '@/components/ui/ModalShell';
import { isPlatformMode } from '@/config/hostMode';
import type { Computer, ComputerCreate, ComputerProviderOption, ResourceTier, WorkspaceQuota } from '@/types/api';

import { useComputerProviders } from '../hooks/useComputers';
import { remoteControlOffered, useThisMac } from '../hooks/useRemoteControl';
import { useTierQuota } from '../hooks/useTierQuota';
import { COMPUTER_NAME_MAX } from './computerName';
import { SshSetup } from './sshSetup';
import { useSshSetup } from './sshSetupState';
import { RemoteControlActionButton, RemoteControlSetup } from './remoteControlSetup';
import { useRemoteControlSetup } from './remoteControlSetupState';
import { PlanUpgradeHint } from './planUpgradeHint';
import {
  computerCapReached,
  defaultProviderKind,
  isProviderLocked,
  providerCountsTowardCap,
  providerLabel,
  providerManagesSpec,
  providerOptions,
  THIS_MAC_OPTION,
  thisMacOption,
  type ThisMacCard,
} from './providerUi';
import { RadioCardGroup, type RadioCardOption } from './radioCardGroup';
import {
  TIER_ORDER,
  TierRadioGroup,
  isTierBlocked,
  needsBiggerPlan,
  offeredTiers,
  tierCapacity,
  tierLabel,
} from './tierUi';

/**
 * Where the new computer comes from: this Mac, the user's own machine over
 * SSH, or a provider that runs it, null while every one on offer is locked.
 */
type Source = 'thisMac' | 'ssh' | { hosted: string | null };

/**
 * The source from the user's pick. Derived rather than seeded: the lists and
 * the quota land after the form opens and can refetch while it is open, and a
 * pick they have since locked falls back to what is open instead of being sent
 * to earn a 403.
 */
function sourceOf(
  picked: string | null,
  providers: readonly ComputerProviderOption[],
  thisMac: ThisMacCard | null,
  atCap: boolean,
): Source {
  if (picked === THIS_MAC_OPTION && thisMac === 'offered') return 'thisMac';
  const option = providers.find((p) => p.kind === picked);
  const kind = option && !isProviderLocked(option, atCap) ? option.kind : defaultProviderKind(providers, atCap);
  return kind === 'ssh' ? 'ssh' : { hosted: kind };
}

interface CreateComputerDialogProps {
  /** The user's computers once the list has answered, so the This Mac card can say it is already one. */
  computers: readonly Computer[] | undefined;
  onClose: () => void;
  onSubmit: (body: ComputerCreate) => void;
  /** The request is in flight. */
  busy: boolean;
  /** The last refusal, already worded for the user. */
  error: string | null;
}

function CreateComputerDialog({ computers, onClose, onSubmit, busy, error }: CreateComputerDialogProps) {
  const { t } = useTranslation();
  const titleId = useId();
  const formId = useId();
  const nameId = useId();
  const [name, setName] = useState('');
  const [picked, setPicked] = useState<string | null>(null);
  const [pickedTier, setPickedTier] = useState<ResourceTier>('standard');
  const errorRef = useRef<HTMLParagraphElement>(null);

  // The SSH summary makes the form taller than the dialog, so a refusal would
  // land below the fold with nothing visible changing.
  useEffect(() => {
    if (error && !busy) errorRef.current?.scrollIntoView?.({ block: 'nearest' });
  }, [error, busy]);

  const { data: providerData } = useComputerProviders({ enabled: true });
  const { data: quota } = useTierQuota({ enabled: true });
  const providers = providerData?.providers ?? [];
  // One provider is no choice: the request then names none and the server
  // uses its default, as it does while the list is still loading.
  const choosesProvider = providers.length > 1;

  const atCap = computerCapReached(quota);

  // This Mac, where the desktop app can pair it and this server can reach it.
  // Once a computer is this Mac the card says so instead of pairing it again,
  // so the card waits for the list to answer that.
  const thisMac = useThisMac({ enabled: true });
  const thisMacCard: ThisMacCard | null = !remoteControlOffered(thisMac.status, providerData) || !computers
    ? null
    : computers.some((c) => thisMac.of(c) !== null) ? 'added' : 'offered';

  const source = sourceOf(picked, providers, thisMacCard, atCap);
  const hosted = typeof source === 'object' ? source.hosted : null;
  // A size the picked provider does not run falls back the same way.
  const offer = offeredTiers(providers.find((p) => p.kind === hosted));
  const tier: ResourceTier =
    offer.tiers.includes(pickedTier) && !isTierBlocked(tierCapacity(quota, pickedTier)) ? pickedTier : 'standard';
  const ssh = useSshSetup(source === 'ssh');
  const mac = useRemoteControlSetup(source === 'thisMac');
  // A machine the user brings is named in its own setup and sized by them, so
  // it has no name field here and its tier is never a pick.
  const own = source === 'thisMac' ? mac : source === 'ssh' ? ssh : null;

  // A size the plan allows that this provider does not run but another open
  // one does (Extra Large, on E2B alone) is named with the way there, in
  // place of the upgrade line: a bigger plan is not what the user lacks.
  const elsewhere = hosted !== null ? tierElsewhere(providers, hosted, offer.tiers, quota, atCap) : null;
  const capLocked = atCap && providers.some((p) => providerCountsTowardCap(p.kind));
  const providerLocked = choosesProvider && providers.some((p) => isProviderLocked(p));
  const tierLocked = !own && !elsewhere && offer.tiers.some((id) => needsBiggerPlan(quota, id));
  // The cap is the lock in the way of the most, so it is the one named.
  const upgradeLead = capLocked
    ? t('computer.capPlanLimited', 'Need more computers than your plan includes?')
    : providerLocked && tierLocked
      ? t('computer.planLimitedBoth', "Need a provider or spec your plan doesn't include?")
      : providerLocked
        ? t('computer.planLimitedProvider', "Need a provider your plan doesn't include?")
        : tierLocked
          ? t('computer.spec.planLimited', 'Need a bigger spec than your plan includes?')
          : null;
  // The body the form would send, or null while it cannot send one. A machine
  // the user brings, this Mac included, sends its setup's part and always names
  // its kind, since the server takes a target only with it.
  const trimmedName = name.trim();
  const fields: ComputerCreate | null = own
    ? own.create && { ...own.create, resource_tier: 'standard', kind: 'ssh' }
    : {
      ...(trimmedName ? { name: trimmedName } : {}),
      resource_tier: tier,
      ...(choosesProvider && hosted ? { kind: hosted } : {}),
    };
  // Every provider on offer locked leaves nothing to create on. The cap locks
  // even a lone provider, which the form shows no choice for.
  const blocked = ((choosesProvider || atCap) && !own && hosted === null) || !fields;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (busy || blocked) return;
    onSubmit(fields);
  };

  const footer = (
    <div className="flex items-center justify-end gap-2">
      <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
        {t('common.cancel')}
      </Button>
      {/* Where this Mac's setup waits on the user (the disclosure, an approval,
          a retry), the button is its next step rather than Add computer. */}
      {source === 'thisMac' && mac.action ? (
        <RemoteControlActionButton state={mac} busy={busy} />
      ) : (
        <Button type="submit" form={formId} disabled={busy || blocked} aria-busy={busy}>
          {own
            ? (busy ? t('computer.adding', 'Adding...') : t('computer.add', 'Add computer'))
            : (busy ? t('computer.creating', 'Creating...') : t('computer.create', 'Create computer'))}
        </Button>
      )}
    </div>
  );

  // The hosted sandboxes come first, then the user's own machines: This Mac,
  // then SSH, both in preview.
  const providerCards = providerOptions(t, providers, atCap).map((o) =>
    // Beside the title, where a lock note would sit; a lock note wins.
    o.value === 'ssh' && !o.note ? { ...o, note: t('computer.ssh.previewLabel', 'Preview') } : o,
  );
  const options: RadioCardOption<string>[] = [
    ...providerCards.filter((o) => providerManagesSpec(o.value)),
    ...(thisMacCard ? [thisMacOption(t, thisMacCard)] : []),
    ...providerCards.filter((o) => !providerManagesSpec(o.value)),
  ];

  return (
    <ModalShell
      labelId={titleId}
      title={t('computer.newComputer', 'New computer')}
      subtitle={t(
        'computer.createDesc',
        'A separate machine with its own files. Workspaces you put on it start and stop with it.',
      )}
      onClose={onClose}
      closeDisabled={busy}
      // Wide enough for three providers side by side, each name on one line.
      width="wide"
      density="form"
      stacked
      footer={footer}
    >
      <form id={formId} onSubmit={submit} className="flex flex-col gap-4">
        {!own && (
          <div className="flex flex-col gap-1.5">
            <label htmlFor={nameId} className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
              {t('computer.nameLabel', 'Name')}{' '}
              <span className="font-normal" style={{ color: 'var(--color-text-tertiary)' }}>{t('common.optional')}</span>
            </label>
            <Input
              id={nameId}
              value={name}
              maxLength={COMPUTER_NAME_MAX}
              onChange={(e) => setName(e.target.value)}
              disabled={busy}
              style={{
                backgroundColor: 'var(--color-bg-input)',
                borderColor: 'var(--color-border-muted)',
                color: 'var(--color-text-primary)',
              }}
            />
          </div>
        )}

        {options.length > 1 && (
          <RadioCardGroup
            options={options}
            value={source === 'thisMac' ? THIS_MAC_OPTION : source === 'ssh' ? 'ssh' : hosted}
            onChange={setPicked}
            label={t('computer.provider.label', 'Provider')}
            // Side by side keeps the form short; a third option joins the row,
            // and four, This Mac among them, go two by two rather than leave
            // one alone or squeeze each name onto two lines.
            columns={options.length === 3 ? 3 : 2}
          />
        )}

        {source === 'thisMac' ? (
          <RemoteControlSetup state={mac} account={thisMac.status?.account ?? ''} busy={busy} nameMaxLength={COMPUTER_NAME_MAX} />
        ) : source === 'ssh' ? (
          <SshSetup state={ssh} busy={busy} nameMaxLength={COMPUTER_NAME_MAX} />
        ) : (
          <TierRadioGroup
            offer={offer}
            value={tier}
            onChange={setPickedTier}
            label={t('computer.tierLabel', 'Computer spec')}
            quota={quota}
          />
        )}

        {elsewhere && (
          <p className="text-xs" data-testid="create-computer-tier-elsewhere" style={{ color: 'var(--color-text-secondary)' }}>
            {t('computer.tierElsewhere', '{{tier}} is available on {{provider}}.', {
              tier: tierLabel(t, elsewhere.tier),
              provider: providerLabel(elsewhere.kind, t),
            })}{' '}
            <button
              type="button"
              className="underline font-medium"
              style={{ color: 'var(--color-text-primary)' }}
              disabled={busy}
              onClick={() => {
                setPicked(elsewhere.kind);
                setPickedTier(elsewhere.tier);
              }}
            >
              {t('computer.switchProvider', 'Switch to {{provider}}', { provider: providerLabel(elsewhere.kind, t) })}
            </button>
          </p>
        )}

        {upgradeLead && <PlanUpgradeHint lead={upgradeLead} testId="create-computer-plan-cta" />}

        {error && !busy && (
          <p ref={errorRef} className="text-sm" role="alert" style={{ color: 'var(--color-icon-danger)' }}>
            {error}
          </p>
        )}
      </form>
    </ModalShell>
  );
}

/**
 * The biggest size the plan has a slot of that `kind` does not run, and the
 * first other open provider that does. A size it lacks or has spent is no
 * reason to switch.
 */
function tierElsewhere(
  providers: readonly ComputerProviderOption[],
  kind: string,
  offered: readonly ResourceTier[],
  quota: WorkspaceQuota | null | undefined,
  atCap: boolean,
): { tier: ResourceTier; kind: string } | null {
  if (!isPlatformMode) return null;
  for (const tier of [...TIER_ORDER].reverse()) {
    const capacity = tierCapacity(quota, tier);
    if (!capacity || isTierBlocked(capacity) || offered.includes(tier)) continue;
    const other = providers.find(
      (p) =>
        p.kind !== kind &&
        providerManagesSpec(p.kind) &&
        !isProviderLocked(p, atCap) &&
        offeredTiers(p).tiers.includes(tier),
    );
    if (other) return { tier, kind: other.kind };
  }
  return null;
}

export default CreateComputerDialog;
