/**
 * DALTON-HOOK(composer-drafts): Dalton keeps composer drafts in
 * `lib/dalton/chatDrafts` (a draft is cleared only once the server accepts
 * the send). Upstream's store (#877, `./store`, `./files`, `./idb`) stays in
 * the tree unconnected: this entry point is the only way the app reaches it,
 * and it hands out inert stand-ins, so upstream's store module never loads,
 * writes nothing to storage, and its sidebar / thread-card draft marks never
 * show. Adopting upstream's drafts is a later follow-up (PIPELINE).
 */
export type { DraftContent, DraftFileMeta } from './types';

/** Upstream's draft key names, kept so its call sites compile unchanged. */
export const draftKeys = {
  thread: (threadId: string) => `thread:${threadId}`,
  newThread: (workspaceId: string) => `new:${workspaceId}`,
  dashboard: 'new:dashboard',
  market: 'new:market',
};

/** Upstream's waiting-draft mark: always none under Dalton's drafts. */
export function useWaitingDraft(_key: string | null): string | null {
  return null;
}

/** Upstream's per-thread draft drop on delete: Dalton's drafts are not kept here. */
export function forgetThreadDraft(_threadId: string): void {}

/** Upstream's sign-out draft clear: nothing of upstream's store to clear. */
export async function composerDraftsClearer(): Promise<() => void> {
  return () => {};
}
