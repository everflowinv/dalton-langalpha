/**
 * The provider vocabulary: what each sandbox backend is called and what
 * stopping a computer on it keeps. One table, read by the create form and by
 * the computer rows, so a provider is described the same way everywhere.
 */
import type { useTranslation } from 'react-i18next';

import type { Computer, ComputerProviderOption } from '@/types/api';

import type { RadioCardOption } from './radioCardGroup';

type Translate = ReturnType<typeof useTranslation>['t'];

/** Labels are product names and read the same in every language. */
const PROVIDER_UI: Partial<Record<string, { label: string; descKey: string }>> = {
  daytona: { label: 'Daytona', descKey: 'computer.provider.daytonaDesc' },
  e2b: { label: 'E2B', descKey: 'computer.provider.e2bDesc' },
  docker: { label: 'Docker', descKey: 'computer.provider.dockerDesc' },
};

// hasOwn: a wire value like 'constructor' must not resolve to a prototype member.
function providerUi(kind: string) {
  return Object.hasOwn(PROVIDER_UI, kind) ? PROVIDER_UI[kind] : undefined;
}

/** A kind this build does not know is shown as the server spells it. */
export function providerLabel(kind: string): string {
  return providerUi(kind)?.label ?? kind;
}

export function providerDescription(t: Translate, kind: string): string {
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
    title: providerLabel(p.kind),
    description: providerDescription(t, p.kind),
    note: isProviderLocked(p) ? t('workspace.quotaNotOnPlan', 'Not on your plan') : null,
    disabled: isProviderLocked(p),
  }));
}
