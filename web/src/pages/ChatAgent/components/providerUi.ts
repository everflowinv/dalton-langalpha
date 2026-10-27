/**
 * The provider vocabulary: what each sandbox backend is called and what
 * stopping a computer on it keeps. One table, read by the create form and by
 * the computer rows, so a provider is described the same way everywhere.
 */
import type { useTranslation } from 'react-i18next';

import type { Computer, ComputerProviderOption } from '@/types/api';

import type { RadioCardOption } from './radioCardGroup';

type Translate = ReturnType<typeof useTranslation>['t'];

interface ProviderUi {
  /** Labels are product names and read the same in every language; a phrase has a key instead. */
  label?: string;
  labelKey?: string;
  descKey: string;
  /** The user's own machine, which they size, power and keep. */
  ownMachine?: true;
}

const PROVIDER_UI: Partial<Record<string, ProviderUi>> = {
  daytona: { label: 'Daytona', descKey: 'computer.provider.daytonaDesc' },
  e2b: { label: 'E2B', descKey: 'computer.provider.e2bDesc' },
  docker: { label: 'Docker', descKey: 'computer.provider.dockerDesc' },
  ssh: { labelKey: 'computer.provider.sshLabel', descKey: 'computer.provider.sshDesc', ownMachine: true },
};

/**
 * A machine the user brings is sized, powered and kept by them, so the app
 * offers no tier, spec change or always-on for it.
 */
export function providerManagesSpec(kind: string): boolean {
  return !providerUi(kind)?.ownMachine;
}

/**
 * Home lives on the primary, which is never removed, so it has to be a machine
 * the deployment can build again; one the user brings may be wiped or gone.
 */
export function providerCanBePrimary(kind: string): boolean {
  return !providerUi(kind)?.ownMachine;
}

/**
 * Removing a machine the user brings stops using it and leaves their files
 * there, and deleting a workspace on one leaves its folder unless the user
 * asks for it to go; on every other provider the disk goes with the computer.
 */
export function providerKeepsFilesOnRemove(kind: string): boolean {
  return !!providerUi(kind)?.ownMachine;
}

/**
 * Our key sits in the machine's `authorized_keys` for as long as we log in
 * there, so removing the computer can take it out; a sandbox has no such line.
 */
export function providerHoldsOurKey(kind: string): boolean {
  return !!providerUi(kind)?.ownMachine;
}

// hasOwn: a wire value like 'constructor' must not resolve to a prototype member.
function providerUi(kind: string) {
  return Object.hasOwn(PROVIDER_UI, kind) ? PROVIDER_UI[kind] : undefined;
}

/** A kind this build does not know is shown as the server spells it. */
export function providerLabel(kind: string, t: Translate): string {
  const ui = providerUi(kind);
  return ui?.labelKey ? t(ui.labelKey) : (ui?.label ?? kind);
}

export function providerDescription(kind: string, t: Translate): string {
  const ui = providerUi(kind);
  return ui ? t(ui.descKey) : '';
}

export function isProviderLocked(provider: ComputerProviderOption): boolean {
  return provider.allowed === false;
}

/** The deployment default when the plan allows it, else the first one it does. */
export function defaultProviderKind(providers: readonly ComputerProviderOption[]): string | null {
  const open = providers.filter((p) => !isProviderLocked(p));
  return (open.find((p) => p.is_default) ?? open[0])?.kind ?? null;
}

/**
 * Whether the rows name each machine's provider: only when there is something
 * to tell apart, either a choice on offer or machines already on more than one.
 */
export function rowsShowProvider(
  providers: readonly ComputerProviderOption[] | undefined,
  computers: readonly Pick<Computer, 'kind'>[],
): boolean {
  return (providers?.length ?? 0) > 1 || new Set(computers.map((c) => c.kind)).size > 1;
}

export function providerOptions(t: Translate, providers: readonly ComputerProviderOption[]): RadioCardOption<string>[] {
  return providers.map((p) => ({
    value: p.kind,
    title: providerLabel(p.kind, t),
    description: providerDescription(p.kind, t),
    note: isProviderLocked(p) ? t('workspace.quotaNotOnPlan', 'Not on your plan') : null,
    disabled: isProviderLocked(p),
  }));
}
