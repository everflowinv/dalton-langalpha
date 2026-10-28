/**
 * Which models belong to one connection (a key, a subscription, a provider of
 * the user's own), for the writers that act on a connection alone: removing
 * it, and saving its model picks. Both take a model out of every preference
 * that names it, and preferences store models by name only, so a name another
 * connection still serves must be left alone.
 */
import type { CustomModelEntry, KeyCandidates } from '@/components/model/types';

/** The manifest's rows (`model_metadata`), before the user's entries are merged over them. */
export type ManifestModels = Readonly<Record<string, { provider?: string } | undefined>>;

export interface ConnectionContext {
  customModels: readonly CustomModelEntry[];
  manifest: ManifestModels;
  /** Every provider the user has connected. */
  connected: ReadonlySet<string>;
  /** Where a turn looks for each provider's key (`key_candidates`). */
  keyCandidates?: Readonly<Record<string, KeyCandidates>>;
}

const nameOf = (cm: CustomModelEntry) => cm.name || cm.model_id;

/** A saved custom entry under another provider took this name, so the name routes there. */
export function takenElsewhere(name: string, provider: string, customModels: readonly CustomModelEntry[]): boolean {
  return customModels.some((cm) => cm.provider !== provider && nameOf(cm) === name);
}

/**
 * Whether `name` still runs once `provider` gives it up: a custom entry under
 * another provider took the name (a provider of the user's own serving a
 * built-in, a coding plan's pick of its brand's model), or a key the server
 * would find for it is still connected: its own provider's (a coding plan's
 * pick that the brand's key runs too), or one the vendor issues as one with it
 * (a region's key, the server's `shares_api_key`).
 */
export function servedElsewhere(name: string, provider: string, ctx: ConnectionContext): boolean {
  if (takenElsewhere(name, provider, ctx.customModels)) return true;
  const own = ctx.manifest[name]?.provider;
  if (!own) return false;
  const holders = ctx.keyCandidates?.[own]?.shared ?? [own];
  return holders.some((s) => s !== provider && ctx.connected.has(s));
}

/** Every model name `provider` serves that leaves the user's lists with it. */
export function modelsLeavingWith(provider: string, ctx: ConnectionContext): Set<string> {
  const names = [
    ...ctx.customModels.filter((cm) => cm.provider === provider).map(nameOf),
    ...Object.entries(ctx.manifest).filter(([, meta]) => meta?.provider === provider).map(([name]) => name),
  ];
  return new Set(names.filter((m) => !servedElsewhere(m, provider, ctx)));
}

/**
 * The manifest's models a connection offers: its own, or for a variant without
 * entries of its own (a coding plan) its brand's. Read off each model's own
 * provider rather than the catalog's grouping, which files a region's models
 * under the top-level brand (`z-ai-cn`'s under `z-ai`), so a variant of the
 * region would find none. A name a custom entry under another provider took
 * routes there, so it is not this connection's to offer.
 */
export function connectionBuiltIns(
  provider: string,
  brand: string,
  manifest: ManifestModels,
  customModels: readonly CustomModelEntry[],
): string[] {
  const under = (p: string) =>
    Object.entries(manifest)
      .filter(([name, meta]) => meta?.provider === p && !takenElsewhere(name, provider, customModels))
      .map(([name]) => name);
  const exact = under(provider);
  if (exact.length > 0 || provider === brand) return exact;
  return under(brand);
}

/**
 * The picks a connection's model picker opens with: what is on the user's
 * list and is this connection's (saved under it, or its own manifest model).
 * Only a connection just made, with nothing of its own on the list yet, starts
 * on every candidate; one the user already manages opens as they left it, even
 * with nothing picked.
 */
export function seedPicks({
  provider,
  candidates,
  savedNames,
  starred,
  manifest,
  justConnected,
}: {
  provider: string;
  candidates: readonly string[];
  /** The names saved as custom entries under this provider. */
  savedNames: readonly string[];
  starred: readonly string[];
  manifest: ManifestModels;
  justConnected: boolean;
}): Set<string> {
  const mine = (m: string) => savedNames.includes(m) || manifest[m]?.provider === provider;
  const kept = starred.filter((m) => candidates.includes(m) && mine(m));
  return new Set(kept.length === 0 && justConnected ? candidates : kept);
}
