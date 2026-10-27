/**
 * Which computer surface is open, as a tiny external store.
 *
 * The Computers dialog is opened from places that share no parent below the
 * page root: the gallery toolbar, a workspace card's machine line, and the disk
 * warnings in the gallery and in a chat. So the dialog mounts once at the root
 * and every opener writes here instead of threading callbacks down.
 */
import { useSyncExternalStore } from 'react';

import { registerAuthReset } from '@/lib/authResets';
import { createValueStore } from '@/lib/valueStore';

/**
 * What the Computers dialog opens onto: its rail, with `computerId`'s card in
 * the middle; one machine's details; or the New computer form, which is then
 * the destination (its cancel closes the panel too), with This Mac picked on
 * it when `pick` says so.
 */
export type ComputersPanelView =
  | { view: 'list'; computerId?: string | null }
  | { view: 'details'; computerId: string }
  | { view: 'create'; pick?: 'thisMac' };

export interface ComputersPanelState {
  /** The Computers dialog, null when closed. */
  panel: ComputersPanelView | null;
  /** Counts the openings, so one made while the dialog shows starts it afresh. */
  opening: number;
  /** The change-spec dialog for one machine, opened on its own. */
  specComputerId: string | null;
}

const INITIAL: ComputersPanelState = { panel: null, opening: 0, specComputerId: null };
const store = createValueStore<ComputersPanelState>(INITIAL);
// Survives the page unmount, so a sign-out must not reopen it for the next account.
registerAuthReset(() => store.set(INITIAL));

function set(next: Partial<ComputersPanelState>) {
  store.set({ ...store.get(), ...next });
}

export function openComputersPanel(panel: ComputersPanelView = { view: 'list' }) {
  set({ panel, opening: store.get().opening + 1 });
}

export function closeComputersPanel() {
  set({ panel: null });
}

export function openComputerSpec(computerId: string) {
  set({ specComputerId: computerId });
}

export function closeComputerSpec() {
  set({ specComputerId: null });
}

export function useComputersPanel(): ComputersPanelState {
  return useSyncExternalStore(store.subscribe, store.get);
}
