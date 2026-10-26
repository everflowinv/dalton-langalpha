/**
 * Create a computer: a name, where it runs, and its spec.
 *
 * Opened over the Computers dialog, which owns the request and scrolls to the
 * new row once it lands. Mounted only while open, so every opening starts
 * from a blank form. The provider and the tier are locked by plan the same
 * way, and either lock brings up the same way out as the spec dialog.
 */
import { useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ModalShell } from '@/components/ui/ModalShell';
import type { ComputerCreate, ResourceTier } from '@/types/api';

import { useComputerProviders } from '../hooks/useComputers';
import { useTierQuota } from '../hooks/useTierQuota';
import { PlanUpgradeHint } from './planUpgradeHint';
import { defaultProviderKind, isProviderLocked, providerOptions } from './providerUi';
import { RadioCardGroup } from './radioCardGroup';
import { TIER_ORDER, TierRadioGroup, isTierBlocked, tierCapacity } from './tierUi';

/** The server's column width for a computer's name. */
export const COMPUTER_NAME_MAX = 255;

interface CreateComputerDialogProps {
  onClose: () => void;
  onSubmit: (body: ComputerCreate) => void;
  /** The request is in flight. */
  busy: boolean;
  /** The last refusal, already worded for the user. */
  error: string | null;
}

function CreateComputerDialog({ onClose, onSubmit, busy, error }: CreateComputerDialogProps) {
  const { t } = useTranslation();
  const titleId = useId();
  const formId = useId();
  const nameId = useId();
  const [name, setName] = useState('');
  const [pickedKind, setPickedKind] = useState<string | null>(null);
  const [pickedTier, setPickedTier] = useState<ResourceTier>('standard');

  const { data: providerData } = useComputerProviders({ enabled: true });
  const { data: quota } = useTierQuota({ enabled: true });
  const providers = providerData?.providers ?? [];
  // One provider is no choice: the request then names none and the server
  // uses its default, as it does while the list is still loading.
  const choosesProvider = providers.length > 1;

  // Derived rather than seeded: the list and the quota land after the form
  // opens and can refetch while it is open, and a pick they have since locked
  // falls back to what is open instead of being sent to earn a 403.
  const picked = providers.find((p) => p.kind === pickedKind);
  const kind = picked && !isProviderLocked(picked) ? picked.kind : defaultProviderKind(providers);
  const tier: ResourceTier = isTierBlocked(tierCapacity(quota, pickedTier)) ? 'standard' : pickedTier;

  const providerLocked = choosesProvider && providers.some(isProviderLocked);
  const tierLocked = TIER_ORDER.some((id) => isTierBlocked(tierCapacity(quota, id)));
  const upgradeLead = providerLocked && tierLocked
    ? t('computer.planLimitedBoth', "Need a provider or spec your plan doesn't include?")
    : providerLocked
      ? t('computer.planLimitedProvider', "Need a provider your plan doesn't include?")
      : tierLocked
        ? t('computer.spec.planLimited', 'Need a bigger spec than your plan includes?')
        : null;
  // Every provider on offer locked leaves nothing to create on.
  const blocked = choosesProvider && kind === null;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (busy || blocked) return;
    const trimmed = name.trim();
    onSubmit({
      ...(trimmed ? { name: trimmed } : {}),
      resource_tier: tier,
      ...(choosesProvider && kind ? { kind } : {}),
    });
  };

  const footer = (
    <div className="flex items-center justify-end gap-2">
      <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
        {t('common.cancel')}
      </Button>
      <Button type="submit" form={formId} disabled={busy || blocked} aria-busy={busy}>
        {busy ? t('computer.creating', 'Creating...') : t('computer.create', 'Create computer')}
      </Button>
    </div>
  );

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
      width="standard"
      density="form"
      stacked
      footer={footer}
    >
      <form id={formId} onSubmit={submit} className="flex flex-col gap-4">
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

        {choosesProvider && (
          <RadioCardGroup
            options={providerOptions(t, providers)}
            value={kind}
            onChange={setPickedKind}
            label={t('computer.provider.label', 'Provider')}
            // Side by side keeps the form short; a third provider joins the row.
            columns={providers.length >= 3 ? 3 : 2}
          />
        )}

        <TierRadioGroup
          value={tier}
          onChange={setPickedTier}
          label={t('computer.tierLabel', 'Computer spec')}
          quota={quota}
        />

        {upgradeLead && <PlanUpgradeHint lead={upgradeLead} testId="create-computer-plan-cta" />}

        {error && !busy && (
          <p className="text-sm" role="alert" style={{ color: 'var(--color-icon-danger)' }}>
            {error}
          </p>
        )}
      </form>
    </ModalShell>
  );
}

export default CreateComputerDialog;
