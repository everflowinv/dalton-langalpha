/**
 * The model picker's rail: one tab per brand, and within a brand one tab per
 * way its models are reached.
 *
 * A subscription's models carry the same names as the brand's keyed ones, so
 * one list of both would repeat every name with a different tag. Split by
 * connection inside the brand, no list repeats a name, and each connection
 * has one payer, so its tag names the tab instead of tagging every row.
 */
import { isSubscription, type Payer } from '@/lib/modelPayer';

export interface ModelConnection {
  /** The payer, or `none`. A subscription's payer names its sign-in, so two
   *  subscriptions are two tabs, while every key the user holds pays alike,
   *  and so does the deployment. */
  id: string;
  models: string[];
  /** Names the tab. None for a model nothing bills, a local server's. */
  payer?: Payer;
}

export interface ModelGroup {
  /** The catalog group, which is also the provider whose mark the rail shows. */
  id: string;
  label: string;
  /** Best payer first. */
  connections: ModelConnection[];
}

// The user's own plans and keys come first: what they pay for themselves is
// what they most likely came to pick.
const RANK = { own_key: 1, server_key: 2, credits: 3 } as const;

function payerRank(payer: Payer | undefined): number {
  if (!payer) return 4;
  return isSubscription(payer) ? 0 : RANK[payer];
}

function rankOf(models: string[], payers: Record<string, Payer>): number {
  return Math.min(...models.map((m) => payerRank(payers[m])));
}

export function modelGroups({ models, payers }: {
  models: Record<string, { models?: string[]; display_name?: string }>;
  payers: Record<string, Payer>;
}): ModelGroup[] {
  const groups: ModelGroup[] = [];
  for (const [group, data] of Object.entries(models)) {
    const byConnection = new Map<string, ModelConnection>();
    for (const m of data.models ?? []) {
      const id = payers[m] ?? 'none';
      const connection = byConnection.get(id) ?? { id, models: [], payer: payers[m] };
      byConnection.set(id, connection);
      if (!connection.models.includes(m)) connection.models.push(m);
    }
    if (byConnection.size === 0) continue;
    const connections = [...byConnection.values()];
    // Array sort is stable, so the catalog order holds within a rank.
    connections.sort((a, b) => rankOf(a.models, payers) - rankOf(b.models, payers));
    groups.push({ id: group, label: data.display_name ?? group, connections });
  }
  const rank = (g: ModelGroup) => rankOf(g.connections.flatMap((c) => c.models), payers);
  return groups.sort((a, b) => rank(a) - rank(b));
}
