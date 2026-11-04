import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react';
import * as z from 'zod/mini';
import { useUser } from '@/hooks/useUser';
import type { QuoteMention } from '@/components/ui/chat-input.types';

export const DRAFT_STORAGE_PREFIX = 'dalton.chatDraft.v2:';
/** Where the pre-quote format (text only) lives; read back as plain text until overwritten. */
export const DRAFT_STORAGE_PREFIX_V1 = 'dalton.chatDraft.v1:';
/**
 * DALTON-HOOK(composer-drafts): where the copy of a send still awaiting its
 * verdict lives — the text, quote chips and attachment presence as they were
 * sent. It is a separate entry from the live draft: whatever the user types,
 * clears or autosaves afterwards never touches it, an acceptance deletes only
 * it, and a definite not-taken puts it back and then removes it. A reload with
 * it still present offers it above the input with the "may already have gone
 * through" notice, beside whatever draft the user has typed since.
 */
export const INFLIGHT_STORAGE_PREFIX = 'dalton.chatInFlight.v1:';
export const DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
export const MAX_DRAFT_BYTES = 100 * 1024; // 100 KB
export const DRAFT_SAVE_DEBOUNCE_MS = 300; // 300 ms

const StoredQuoteOriginSchema = z.union([
  z.object({
    kind: z.literal('file'),
    path: z.string(),
    label: z.string(),
    // Null is what QuoteOrigin allows (a file quoted without lines read back);
    // rejecting it here would drop the whole stored draft on one such quote.
    lineStart: z.optional(z.nullable(z.number())),
    lineEnd: z.optional(z.nullable(z.number())),
    lineCount: z.number(),
  }),
  z.object({
    kind: z.literal('message'),
    speaker: z.string(),
    message: z.optional(z.string()),
  }),
]);

const StoredDraftQuoteSchema = z.object({
  kind: z.literal('quote'),
  token: z.string(),
  snippet: z.string(),
  reply: z.optional(z.string()),
  from: StoredQuoteOriginSchema,
});

export const StoredChatDraftSchema = z.object({
  text: z.string(),
  savedAt: z.number(),
  /** Quote mentions held in the draft, token included, so a restore brings the chips back. */
  quotes: z.optional(z.array(StoredDraftQuoteSchema)),
  /** The draft had attachments when it was saved; they are not kept, so a restore says to add them again. */
  hadAttachments: z.optional(z.boolean()),
});

export type StoredChatDraft = z.output<typeof StoredChatDraftSchema>;

/** A send still awaiting its verdict, as it went out (see INFLIGHT_STORAGE_PREFIX). */
export const StoredInFlightCopySchema = z.object({
  text: z.string(),
  savedAt: z.number(),
  quotes: z.optional(z.array(StoredDraftQuoteSchema)),
  hadAttachments: z.optional(z.boolean()),
});

export type StoredInFlightCopy = z.output<typeof StoredInFlightCopySchema>;

/** The rich parts of a draft beyond its text, read at save time from the composer. */
export interface DraftParts {
  quotes?: QuoteMention[];
  hadAttachments?: boolean;
}

export interface SaveDraftOptions {
  parts?: DraftParts;
}

interface DraftController {
  getDraftKey: () => string | undefined;
  getUserId: () => string;
  cancelAndClearInput: () => void;
}

const activeControllers = new Set<DraftController>();

function normalizeUserId(userId?: string | null): string {
  const trimmed = typeof userId === 'string' ? userId.trim() : '';
  return trimmed || 'anon';
}

export function buildDraftStorageKey(
  draftKey: string,
  userId?: string | null,
  prefix: string = DRAFT_STORAGE_PREFIX,
): string {
  return `${prefix}${normalizeUserId(userId)}:${draftKey}`;
}

function getLocalStorage(): Storage | null {
  try {
    if (typeof window === 'undefined' || !window.localStorage) {
      return null;
    }
    return window.localStorage;
  } catch {
    return null;
  }
}

function getUtf8ByteLength(text: string): number {
  try {
    if (typeof TextEncoder !== 'undefined') {
      return new TextEncoder().encode(text).byteLength;
    }
  } catch {
    // Fallback below if TextEncoder is unavailable.
  }
  return unescape(encodeURIComponent(text)).length;
}

function safeRemoveItem(storage: Storage, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    // Ignore storage removal errors.
  }
}

function parseStoredDraft(raw: string): StoredChatDraft | null {
  try {
    const json: unknown = JSON.parse(raw);
    const parsed = StoredChatDraftSchema.safeParse(json);
    if (!parsed.success || !Number.isFinite(parsed.data.savedAt)) {
      return null;
    }
    return parsed.data;
  } catch {
    return null;
  }
}

function clearStorageEntry(
  draftKey: string | null | undefined,
  userId?: string | null,
): void {
  if (!draftKey) return;
  const storage = getLocalStorage();
  if (!storage) return;
  if (userId !== undefined) {
    safeRemoveItem(storage, buildDraftStorageKey(draftKey, userId));
    // A v1 entry under the same id goes too, so a cleared draft cannot come back on the next read.
    safeRemoveItem(storage, buildDraftStorageKey(draftKey, userId, DRAFT_STORAGE_PREFIX_V1));
    return;
  }
  safeRemoveItem(storage, buildDraftStorageKey(draftKey, 'anon'));
  safeRemoveItem(storage, buildDraftStorageKey(draftKey, 'anon', DRAFT_STORAGE_PREFIX_V1));
  try {
    const suffix = `:${draftKey}`;
    const prefixes = [DRAFT_STORAGE_PREFIX, DRAFT_STORAGE_PREFIX_V1];
    const matchingKeys: string[] = [];
    for (let i = 0; i < storage.length; i += 1) {
      const k = storage.key(i);
      if (k && prefixes.some((p) => k.startsWith(p)) && k.endsWith(suffix)) {
        matchingKeys.push(k);
      }
    }
    for (const k of matchingKeys) {
      safeRemoveItem(storage, k);
    }
  } catch {
    // Ignore storage enumeration errors.
  }
}

function loadStoredDraft(
  draftKey: string | null | undefined,
  userId?: string | null,
  now: number = Date.now(),
): StoredChatDraft | null {
  if (!draftKey) return null;
  const storage = getLocalStorage();
  if (!storage) return null;
  // v2 first; a v1 entry (text only) is the draft's earlier self, restored as plain text.
  const read = (prefix: string): StoredChatDraft | null => {
    const storageKey = buildDraftStorageKey(draftKey!, userId, prefix);
    try {
      const raw = storage.getItem(storageKey);
      if (!raw) return null;
      const parsed = parseStoredDraft(raw);
      if (
        !parsed ||
        !parsed.text ||
        !parsed.text.trim() ||
        now - parsed.savedAt > DRAFT_TTL_MS
      ) {
        safeRemoveItem(storage, storageKey);
        return null;
      }
      return parsed;
    } catch {
      safeRemoveItem(storage, storageKey);
      return null;
    }
  };
  return read(DRAFT_STORAGE_PREFIX) ?? read(DRAFT_STORAGE_PREFIX_V1);
}

/**
 * Load a saved composer draft for `(userId, draftKey)`.
 * Validates with Zod and deletes entries that are corrupt, empty, or older than 7 days.
 */
export function load(
  draftKey: string | null | undefined,
  userId?: string | null,
  now: number = Date.now(),
): string {
  return loadStoredDraft(draftKey, userId, now)?.text ?? '';
}

/**
 * Remove a composer draft for `(userId, draftKey)` and cancel any pending
 * debounced save on mounted inputs bound to the same `draftKey`.
 */
export function clear(
  draftKey: string | null | undefined,
  userId?: string | null,
): void {
  if (!draftKey) return;
  clearStorageEntry(draftKey, userId);
  // DALTON-HOOK(composer-drafts): an explicit reset (New Chat) drops the
  // unresolved send's copy with the draft, as the single-entry model did.
  clearInFlightCopy(draftKey, userId);
  const normalizedTargetUser =
    userId !== undefined ? normalizeUserId(userId) : null;
  for (const controller of activeControllers) {
    if (
      controller.getDraftKey() === draftKey &&
      (normalizedTargetUser === null ||
        controller.getUserId() === normalizedTargetUser)
    ) {
      clearStorageEntry(draftKey, controller.getUserId());
      clearInFlightCopy(draftKey, controller.getUserId());
      controller.cancelAndClearInput();
    }
  }
}

/**
 * Save a composer draft for `(userId, draftKey)`.
 * Clears the stored draft when `text` is empty or the stored payload exceeds
 * 100 KB (so an older draft under the same key cannot resurrect), and silently
 * degrades if `localStorage` is unavailable or full.
 */
export function save(
  draftKey: string | null | undefined,
  text: string,
  userId?: string | null,
  now: number = Date.now(),
  options?: SaveDraftOptions,
): boolean {
  if (!draftKey) return false;
  if (!text || !text.trim()) {
    clearStorageEntry(draftKey, userId ?? 'anon');
    return false;
  }
  const parts = options?.parts;
  const payload: StoredChatDraft = {
    text,
    savedAt: now,
    ...(parts?.quotes?.length ? { quotes: parts.quotes } : {}),
    ...(parts?.hadAttachments ? { hadAttachments: true } : {}),
  };
  let serialized: string;
  try {
    serialized = JSON.stringify(payload);
  } catch {
    clearStorageEntry(draftKey, userId ?? 'anon');
    return false;
  }
  if (getUtf8ByteLength(serialized) > MAX_DRAFT_BYTES) {
    clearStorageEntry(draftKey, userId ?? 'anon');
    return false;
  }
  const storage = getLocalStorage();
  if (!storage) return false;
  const storageKey = buildDraftStorageKey(draftKey, userId);
  try {
    storage.setItem(storageKey, serialized);
    return true;
  } catch {
    return false;
  }
}

// DALTON-HOOK(composer-drafts): the in-flight copy's own storage slot — see
// INFLIGHT_STORAGE_PREFIX. Deliberately narrow: only the send that wrote it
// (or its settle) calls these; the live draft's save/clear never does.

function parseInFlightCopy(raw: string): StoredInFlightCopy | null {
  try {
    const json: unknown = JSON.parse(raw);
    const parsed = StoredInFlightCopySchema.safeParse(json);
    if (!parsed.success || !Number.isFinite(parsed.data.savedAt)) return null;
    return parsed.data;
  } catch {
    return null;
  }
}

export function buildInFlightStorageKey(draftKey: string, userId?: string | null): string {
  return `${INFLIGHT_STORAGE_PREFIX}${normalizeUserId(userId)}:${draftKey}`;
}

export function saveInFlightCopy(
  draftKey: string | null | undefined,
  text: string,
  userId?: string | null,
  parts: DraftParts = {},
  now: number = Date.now(),
): boolean {
  if (!draftKey || !text.trim()) return false;
  const payload: StoredInFlightCopy = {
    text,
    savedAt: now,
    ...(parts.quotes?.length ? { quotes: parts.quotes } : {}),
    ...(parts.hadAttachments ? { hadAttachments: true } : {}),
  };
  const storage = getLocalStorage();
  if (!storage) return false;
  try {
    storage.setItem(buildInFlightStorageKey(draftKey, userId), JSON.stringify(payload));
    return true;
  } catch {
    return false;
  }
}

export function loadInFlightCopy(
  draftKey: string | null | undefined,
  userId?: string | null,
  now: number = Date.now(),
): StoredInFlightCopy | null {
  if (!draftKey) return null;
  const storage = getLocalStorage();
  if (!storage) return null;
  const storageKey = buildInFlightStorageKey(draftKey, userId);
  try {
    const raw = storage.getItem(storageKey);
    if (!raw) return null;
    const parsed = parseInFlightCopy(raw);
    if (!parsed || !parsed.text.trim() || now - parsed.savedAt > DRAFT_TTL_MS) {
      safeRemoveItem(storage, storageKey);
      return null;
    }
    return parsed;
  } catch {
    safeRemoveItem(storage, storageKey);
    return null;
  }
}

export function clearInFlightCopy(
  draftKey: string | null | undefined,
  userId?: string | null,
): void {
  if (!draftKey) return;
  const storage = getLocalStorage();
  if (!storage) return;
  safeRemoveItem(storage, buildInFlightStorageKey(draftKey, userId));
}

/** Moves the in-flight copy with a send whose key changed under it (ws:new → thread). */
export function migrateInFlightCopy(
  fromKey: string | null | undefined,
  toKey: string | null | undefined,
  fromUser?: string | null,
  toUser?: string | null,
): void {
  if (!fromKey || !toKey || fromKey === toKey) return;
  const copy = loadInFlightCopy(fromKey, fromUser);
  if (!copy) return;
  saveInFlightCopy(toKey, copy.text, toUser, copy, copy.savedAt);
  clearInFlightCopy(fromKey, fromUser);
}

/**
 * Scan `localStorage` for draft and in-flight keys and remove any expired
 * (> 7 days) or malformed entries.
 */
export function pruneExpired(now: number = Date.now()): number {
  const storage = getLocalStorage();
  if (!storage) return 0;
  let removed = 0;
  try {
    const keys: string[] = [];
    const prefixes = [DRAFT_STORAGE_PREFIX, DRAFT_STORAGE_PREFIX_V1, INFLIGHT_STORAGE_PREFIX];
    for (let i = 0; i < storage.length; i += 1) {
      const k = storage.key(i);
      if (k && prefixes.some((p) => k.startsWith(p))) {
        keys.push(k);
      }
    }
    for (const key of keys) {
      try {
        const raw = storage.getItem(key);
        if (!raw) continue;
        const isCopy = key.startsWith(INFLIGHT_STORAGE_PREFIX);
        const parsed = isCopy
          ? parseInFlightCopy(raw) as { text?: string; savedAt?: number } | null
          : parseStoredDraft(raw);
        if (
          !parsed ||
          !parsed.text?.trim() ||
          now - (parsed.savedAt ?? 0) > DRAFT_TTL_MS
        ) {
          safeRemoveItem(storage, key);
          removed += 1;
        }
      } catch {
        safeRemoveItem(storage, key);
        removed += 1;
      }
    }
  } catch {
    // Ignore storage enumeration errors.
  }
  return removed;
}

export const clearExpired = pruneExpired;

function isNewWorkspaceDraftKey(key?: string): boolean {
  return typeof key === 'string' && /^ws:[^:]+:new$/.test(key);
}

function isThreadDraftKey(key?: string): boolean {
  return (
    typeof key === 'string' &&
    key.startsWith('thread:') &&
    key.length > 'thread:'.length
  );
}

export interface UseComposerDraftOptions {
  draftKey?: string;
  prefillMessage?: string;
  onDraftKeySwitch?: () => void;
  /**
   * The rich parts of the draft as it stands now, read when a save flushes —
   * so what a restore brings back (quote chips, the attachments-lost hint)
   * tracks the draft at the moment it was kept, not when the save was queued.
   */
  getDraftParts?: () => DraftParts;
  /**
   * DALTON-HOOK(composer-drafts): re-seats a failed send's text and quote
   * chips into the composer, collision-safe against quotes added since (one
   * may have taken the same token). Called by the failure settle only when no
   * host giveBack took the send back (the send reports that through its own
   * options; see PendingSendHandle.markHostReturned).
   */
  onSendReturned?: (sentText: string, parts: DraftParts) => void;
}

export interface PendingSendHandle {
  /**
   * The fallback settle: the send's promise resolved. `true` settles the send
   * without touching anything stored — only the server holding the turn
   * (reported through `acceptSend`) may delete the send's copy, and a `true`
   * that reported nothing leaves it standing, so a reload offers it with the
   * "may already have gone through" notice; `false` puts the copy back whole.
   * A parked send (see `handOffToResend`) ignores this.
   */
  finalizeSend: (succeeded: boolean) => void;
  /**
   * The server took this send — its own first frame, a duplicate adopted, a
   * steer it was merged into, or (for a parked send) the resend's acceptance.
   * The only settle that deletes the send's copy; the live draft and whatever
   * the box holds are not its business. May still fire after a
   * `restoreSend` (a manual Retry carrying the original acceptance): it then
   * deletes the copy if it still stands, and nothing else.
   */
  acceptSend: () => void;
  /**
   * A drop scheduled this send's own automatic resend: from here the promise's
   * value settles nothing, and only `acceptSend` (the resend's acceptance,
   * riding the resent attempts' options) or `restoreSend` (a definite
   * not-taken: the chain gave up, or a stop cancelled the backoff) may settle.
   */
  handOffToResend: () => void;
  /**
   * The send was definitely not taken — settle it as failed and put the copy
   * back whole (text, quote chips, the attachments-again notice), parked or
   * not, then remove it. A host that already took the send back through its
   * `giveBack` (see `markHostReturned`) keeps the input to itself: only the
   * copy is removed there.
   */
  restoreSend: () => void;
  /**
   * The host returned this send itself (the chat's returnedDraft → its
   * giveBack re-seats text, quotes and files) — the failure settle then
   * removes only the copy and leaves the input to that restore.
   */
  markHostReturned: () => void;
}

interface InFlightSendState {
  draftKey?: string;
  userId: string;
  sentText: string;
  settled: boolean;
  /** True once handOffToResend parked it: acceptance or a forced restore settle it from there. */
  handedOff?: boolean;
  /** True once the host reported taking the refused send back itself. */
  hostReturned?: boolean;
  /** True once the send's copy was deleted (by an acceptance). */
  cleared?: boolean;
  /** The draft's rich parts at send: what a restore brings back. */
  parts: DraftParts;
}

export function settleSendResult(
  pending: PendingSendHandle,
  invoke: () => void | boolean | Promise<void | boolean>,
): void {
  let result: void | boolean | Promise<void | boolean>;
  try {
    result = invoke();
  } catch {
    pending.finalizeSend(false);
    return;
  }
  if (
    result &&
    typeof (result as Promise<void | boolean>).then === 'function'
  ) {
    (result as Promise<void | boolean>).then(
      (resolved) => pending.finalizeSend(resolved === true),
      () => pending.finalizeSend(false),
    );
    return;
  }
  pending.finalizeSend(result === true);
}

export interface UseComposerDraftResult {
  message: string;
  setMessage: Dispatch<SetStateAction<string>>;
  /** Quotes the restored draft carried, a fresh array per restore; seed them into the composer's mentions once. */
  restoredQuotes: QuoteMention[];
  /** The restored draft had attachments, which are not kept; the composer says to add them again. */
  restoredHadAttachments: boolean;
  /**
   * DALTON-HOOK(composer-drafts): a send whose verdict never arrived — its
   * copy as it went out, kept beside the live draft (see
   * INFLIGHT_STORAGE_PREFIX) and offered above the box until the user puts it
   * back or drops it. Null while a send of this session owns the slot.
   */
  inFlightCopy: StoredInFlightCopy | null;
  /** Seats the offered copy back into the box, ahead of what is typed since, and removes it. */
  restoreInFlightCopy: () => void;
  /** Drops the offered copy: the user says it is gone, sent or unwanted. */
  discardInFlightCopy: () => void;
  clearCurrentDraft: () => void;
  /**
   * DALTON-HOOK(composer-drafts): something beside the text changed (an
   * attachment was added or removed) that the stored draft carries — saves the
   * live draft on the same debounce.
   */
  touchDraft: () => void;
  beginSend: () => PendingSendHandle;
  submitWithDraft: (
    invoke: (
      onAccepted: () => void,
      onHandedOff: () => void,
      onRestore: () => void,
      onHostReturned: () => void,
    ) => void | boolean | Promise<void | boolean>,
  ) => void;
}

/**
 * Manages the composer text state backed by `localStorage` when `draftKey` is provided:
 * - Restores on mount and after `draftKey` changes unless `prefillMessage` is active.
 * - Debounces writes by 300ms on input changes and clears immediately when emptied by user.
 * - Synchronously flushes pending edits before `draftKey` changes and on unmount.
 * - Migrates post-send continuation text when transitioning from `ws:<id>:new` to `thread:<id>`.
 */
export function useComposerDraft({
  draftKey,
  prefillMessage,
  onDraftKeySwitch,
  getDraftParts,
  onSendReturned,
}: UseComposerDraftOptions): UseComposerDraftResult {
  const { user } = useUser({ enabled: Boolean(draftKey) });
  const userId = normalizeUserId(user?.user_id);

  const [message, setMessageState] = useState<string>(() => {
    if (prefillMessage) return prefillMessage;
    if (!draftKey) return '';
    return load(draftKey, userId);
  });
  // DALTON-HOOK(composer-drafts): the unresolved send's copy, offered above
  // the box — the live draft in the box never merges with it.
  const [inFlightCopy, setInFlightCopyState] = useState<StoredInFlightCopy | null>(() => {
    if (prefillMessage || !draftKey) return null;
    return loadInFlightCopy(draftKey, userId);
  });
  const [restoredQuotes, setRestoredQuotesState] = useState<QuoteMention[]>(() => {
    if (prefillMessage || !draftKey) return [];
    return loadStoredDraft(draftKey, userId)?.quotes ?? [];
  });
  const [restoredHadAttachments, setRestoredHadAttachmentsState] = useState<boolean>(() => {
    if (prefillMessage || !draftKey) return false;
    return loadStoredDraft(draftKey, userId)?.hadAttachments === true;
  });

  const latestMessageRef = useRef<string>(message);
  const dirtyRef = useRef<boolean>(false);
  const writtenSinceMountOrSendRef = useRef<boolean>(false);
  const hasSentOnKeyRef = useRef<boolean>(false);
  const inFlightSendRef = useRef<InFlightSendState | null>(null);
  // The latest send per (user, key). Kept after that send settles, so an older send on
  // the same key that completes later still knows it was superseded.
  const latestSendByKeyRef = useRef(new Map<string, InFlightSendState>());
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeDraftKeyRef = useRef<string | undefined>(draftKey);
  const activeUserIdRef = useRef<string>(userId);
  const prefillMessageRef = useRef<string | undefined>(prefillMessage);
  const hadPrefillRef = useRef<boolean>(Boolean(prefillMessage));
  const onDraftKeySwitchRef = useRef<(() => void) | undefined>(onDraftKeySwitch);
  const getDraftPartsRef = useRef<(() => DraftParts) | undefined>(getDraftParts);
  const onSendReturnedRef = useRef<((sentText: string, parts: DraftParts) => void) | undefined>(onSendReturned);
  /** Mirrors `restoredHadAttachments` from its stored initial value, for a save to keep the attachments-again notice standing. */
  const restoredHadAttachmentsRef = useRef<boolean>(restoredHadAttachments);
  const prevTargetRef = useRef<{ draftKey?: string; userId: string }>({
    draftKey,
    userId,
  });

  /** Applies what a restore brought back; `null` is every reset point (sent, cleared, switched). */
  const applyRestored = useCallback(
    (record: { quotes?: QuoteMention[]; hadAttachments?: boolean } | null) => {
      setRestoredQuotesState(record?.quotes ?? []);
      restoredHadAttachmentsRef.current = record?.hadAttachments === true;
      setRestoredHadAttachmentsState(restoredHadAttachmentsRef.current);
    },
    [],
  );

  /**
   * The parts a save of the live composer writes: what it holds now, and the
   * attachments-again notice a failure restore left standing — a later edit
   * must not drop it before the files are back beside the draft.
   */
  const partsForSave = useCallback((): DraftParts => {
    const parts = getDraftPartsRef.current?.() ?? {};
    return parts.hadAttachments || restoredHadAttachmentsRef.current
      ? { ...parts, hadAttachments: true }
      : parts;
  }, []);

  useLayoutEffect(() => {
    activeDraftKeyRef.current = draftKey;
    activeUserIdRef.current = userId;
    prefillMessageRef.current = prefillMessage;
    if (prefillMessage) {
      hadPrefillRef.current = true;
    }
    onDraftKeySwitchRef.current = onDraftKeySwitch;
    getDraftPartsRef.current = getDraftParts;
    onSendReturnedRef.current = onSendReturned;
  });

  useLayoutEffect(() => {
    const controller: DraftController = {
      getDraftKey: () => activeDraftKeyRef.current,
      getUserId: () => activeUserIdRef.current,
      cancelAndClearInput: () => {
        if (timerRef.current !== null) {
          clearTimeout(timerRef.current);
          timerRef.current = null;
        }
        dirtyRef.current = false;
        writtenSinceMountOrSendRef.current = false;
        latestMessageRef.current = '';
        applyRestored(null);
        setMessageState('');
      },
    };
    activeControllers.add(controller);
    return () => {
      activeControllers.delete(controller);
    };
  }, [applyRestored]);

  const scheduleSave = useCallback((nextText: string) => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const key = activeDraftKeyRef.current;
    const uid = activeUserIdRef.current;
    if (!key) {
      dirtyRef.current = false;
      return;
    }
    if (!nextText || !nextText.trim()) {
      dirtyRef.current = false;
      writtenSinceMountOrSendRef.current = false;
      clearStorageEntry(key, uid);
      return;
    }
    dirtyRef.current = true;
    writtenSinceMountOrSendRef.current = true;
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      if (
        dirtyRef.current &&
        activeDraftKeyRef.current === key &&
        activeUserIdRef.current === uid
      ) {
        // Parts read at flush: what came back through a restore or giveBack
        // has landed by the time the debounce ends. The live draft only —
        // a send still out keeps its own copy under a separate key.
        save(key, latestMessageRef.current, uid, undefined, { parts: partsForSave() });
        dirtyRef.current = false;
      }
    }, DRAFT_SAVE_DEBOUNCE_MS);
  }, [partsForSave]);

  const setMessage: Dispatch<SetStateAction<string>> = useCallback(
    (action) => {
      const next =
        typeof action === 'function'
          ? action(latestMessageRef.current)
          : action;
      latestMessageRef.current = next;
      setMessageState(next);
      scheduleSave(next);
    },
    [scheduleSave],
  );

  /**
   * DALTON-HOOK(composer-drafts): something beside the text changed (an
   * attachment was added or removed) that the live draft carries — saves on
   * the same debounce. A send still out is untouched by it: its copy lives
   * under its own key.
   */
  const touchDraft = useCallback(() => {
    if (activeDraftKeyRef.current && latestMessageRef.current.trim()) {
      scheduleSave(latestMessageRef.current);
    }
  }, [scheduleSave]);

  const clearCurrentDraft = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    dirtyRef.current = false;
    writtenSinceMountOrSendRef.current = false;
    latestMessageRef.current = '';
    applyRestored(null);
    if (activeDraftKeyRef.current) {
      clearStorageEntry(activeDraftKeyRef.current, activeUserIdRef.current);
    }
    setMessageState('');
  }, [applyRestored]);

  /**
   * DALTON-HOOK(composer-drafts): seats the offered unresolved-send copy back
   * into the box — ahead of whatever is typed since, its quote chips with it —
   * and removes the copy. The live draft that lands in the box is what the
   * next save keeps.
   */
  const restoreInFlightCopy = useCallback(() => {
    const key = activeDraftKeyRef.current;
    const uid = activeUserIdRef.current;
    const copy = key ? loadInFlightCopy(key, uid) : null;
    if (!key || !copy) {
      clearInFlightCopy(key, uid);
      setInFlightCopyState(null);
      return;
    }
    applyRestored(copy);
    let merged: string;
    const restore = onSendReturnedRef.current;
    if (restore) {
      restore(copy.text, { quotes: copy.quotes, hadAttachments: copy.hadAttachments });
      merged = latestMessageRef.current;
    } else {
      const current = latestMessageRef.current;
      const sent = copy.text;
      merged = !current.trim() || current === sent || current.startsWith(`${sent}\n\n`) || current.endsWith(`\n\n${sent}`)
        ? (current.trim() ? current : sent)
        : `${sent}\n\n${current}`;
      latestMessageRef.current = merged;
      setMessageState(merged);
    }
    // Seat the live draft at once, then remove the copy: a refresh between
    // the two never finds the words in neither place.
    if (merged.trim()) {
      save(key, merged, uid, undefined, { parts: { quotes: copy.quotes, hadAttachments: copy.hadAttachments } });
    }
    clearInFlightCopy(key, uid);
    setInFlightCopyState(null);
  }, [applyRestored]);

  /** Drops the offered copy: the user says it is gone, sent or unwanted. */
  const discardInFlightCopy = useCallback(() => {
    clearInFlightCopy(activeDraftKeyRef.current, activeUserIdRef.current);
    setInFlightCopyState(null);
  }, []);

  const beginSend = useCallback((): PendingSendHandle => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const sentText = latestMessageRef.current;
    // The draft as it goes out — a restore after a failed send (this page or a
    // reload) brings back the quotes it carried, not whatever is typed since.
    // The sticky notice counts: a resend of a draft whose files never came
    // back still owes the attachments-again notice should it fail again.
    const parts = partsForSave();
    const inFlight: InFlightSendState = {
      draftKey: activeDraftKeyRef.current,
      userId: activeUserIdRef.current,
      sentText,
      settled: false,
      parts,
    };
    inFlightSendRef.current = inFlight;
    if (inFlight.draftKey) {
      latestSendByKeyRef.current.set(`${inFlight.userId ?? ''}|${inFlight.draftKey}`, inFlight);
    }

    // DALTON-HOOK(composer-drafts): the send's copy goes under its own key —
    // whatever the user types, clears or autosaves afterwards cannot touch it
    // — and the live draft goes with the box: empty until they type again.
    if (inFlight.draftKey && sentText.trim()) {
      saveInFlightCopy(inFlight.draftKey, sentText, inFlight.userId, parts);
    }
    if (inFlight.draftKey) {
      clearStorageEntry(inFlight.draftKey, inFlight.userId);
    }
    // The slot belongs to the active send now: no banner for it.
    setInFlightCopyState(null);
    dirtyRef.current = false;
    writtenSinceMountOrSendRef.current = false;
    hasSentOnKeyRef.current = true;
    latestMessageRef.current = '';
    applyRestored(null);
    setMessageState('');

    /**
     * DALTON-HOOK(composer-drafts): the two settlement rules, over the copy.
     * An acceptance deletes the copy alone; a definite not-taken seats it back
     * into the box (unless the host's giveBack owns that) and removes it;
     * everything else leaves it standing, so a reload offers it beside the
     * live draft with the "may already have gone through" notice.
     */
    const settle = (succeeded: boolean, fromAcceptance = false, force = false) => {
      // A newer send on this key may have superseded this one while it was in
      // flight (a steer sent mid-stream, say): the key's copy and the input
      // are that send's now — this settle touches neither, or the first
      // send's completion would delete or restore the second send's copy.
      // Supersession is per key: a newer send on another key (the market panel's key moves
      // from its workspace form to the new thread's once the thread exists) does not own
      // this send's copy, so this send still settles its own key.
      const latestOnKey = inFlight.draftKey
        ? latestSendByKeyRef.current.get(`${inFlight.userId ?? ''}|${inFlight.draftKey}`)
        : undefined;
      const superseded = Boolean(latestOnKey && latestOnKey !== inFlight);
      if (inFlight.settled) {
        // Settled, but an acceptance may still arrive for it — a manual Retry
        // of the failed reply carried the original acceptance — and delete the
        // copy if it still stands. If a restore already seated it back and
        // removed it, there is nothing to delete; the box and its live draft
        // are the user's either way.
        if (fromAcceptance && !inFlight.cleared && !superseded) {
          inFlight.cleared = true;
          clearInFlightCopy(inFlight.draftKey, inFlight.userId);
        }
        return;
      }
      // A parked send answers only to its resend's acceptance, or to the
      // definite not-taken (`force`): the chain gave up, or a stop cancelled
      // the backoff. The promise's own value settles nothing while the resend
      // may still be the copy the server keeps.
      if (inFlight.handedOff && !fromAcceptance && !force) return;
      inFlight.settled = true;
      if (superseded) return;
      if (inFlightSendRef.current === inFlight) inFlightSendRef.current = null;

      if (succeeded) {
        // The server holds the turn: the copy goes, and nothing else —
        // whatever was typed since is the live draft's business. A `true`
        // that reported nothing leaves the copy standing: unknown.
        if (fromAcceptance) {
          inFlight.cleared = true;
          clearInFlightCopy(inFlight.draftKey, inFlight.userId);
        }
        return;
      }
      const sendDraftKey = inFlight.draftKey;
      const sendUserId = inFlight.userId;
      if (!sendDraftKey) {
        return;
      }
      if (inFlight.hostReturned) {
        // The host's giveBack owns the box. Flush what the box holds as the
        // live draft at once (its debounce may not have fired), and only then
        // remove the copy: a refresh in between never finds neither. The
        // giveBack that lands next seats the send's words over the live draft.
        if (latestMessageRef.current.trim()) {
          save(sendDraftKey, latestMessageRef.current, sendUserId, undefined, { parts: partsForSave() });
        }
        clearInFlightCopy(sendDraftKey, sendUserId);
        return;
      }
      if (activeDraftKeyRef.current !== sendDraftKey) {
        // The box moved to another draft under the send. Seating the copy
        // here would cross threads: it stays where it is, and returning to
        // that thread offers it back above the box.
        return;
      }
      // Definitely not taken: the copy goes back into the box — ahead of what
      // was typed since, its quote chips renumbered against what the box holds
      // now — then away.
      hasSentOnKeyRef.current = false;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      writtenSinceMountOrSendRef.current = true;
      applyRestored({ quotes: inFlight.parts.quotes, hadAttachments: inFlight.parts.hadAttachments });
      let merged: string;
      const restore = onSendReturnedRef.current;
      if (restore) {
        restore(sentText, inFlight.parts);
        // The restore wrote through setMessage, so this is what the box now
        // holds — seat the live draft at once with it (the debounce that
        // follows rewrites it with the fully re-seated chips), then remove
        // the copy: a refresh between the two never finds the words in
        // neither place.
        merged = latestMessageRef.current;
      } else {
        const current = latestMessageRef.current;
        merged = !sentText.trim()
          ? current
          : !current.trim() || current === sentText || current.startsWith(`${sentText}\n\n`) || current.endsWith(`\n\n${sentText}`)
            ? (current.trim() ? current : sentText)
            : `${sentText}\n\n${current}`;
        latestMessageRef.current = merged;
        setMessageState(merged);
      }
      dirtyRef.current = false;
      if (merged.trim()) {
        save(sendDraftKey, merged, sendUserId, undefined, { parts: inFlight.parts });
      }
      clearInFlightCopy(sendDraftKey, sendUserId);
    };

    return {
      finalizeSend(succeeded: boolean) {
        settle(succeeded);
      },
      acceptSend() {
        settle(true, true);
      },
      handOffToResend() {
        if (inFlight.settled) return;
        inFlight.handedOff = true;
      },
      restoreSend() {
        settle(false, false, true);
      },
      markHostReturned() {
        inFlight.hostReturned = true;
      },
    };
  }, [applyRestored, partsForSave]);

  const submitWithDraft = useCallback(
    (
      invoke: (
        onAccepted: () => void,
        onHandedOff: () => void,
        onRestore: () => void,
        onHostReturned: () => void,
      ) => void | boolean | Promise<void | boolean>,
    ) => {
      const pending = beginSend();
      // The send path reports through these: acceptance (a run started, a
      // steer was taken, a duplicate adopted — the only thing that may delete
      // the stored draft), a handoff to the send's own automatic resend (the
      // resend's acceptance settles from there), a definite not-taken (a stop
      // cancelled the backoff, the chain gave up, the queue was dropped — the
      // draft comes back whole), and the host having taken a refused send back
      // itself (the settle then leaves the input to its giveBack). The
      // promise's resolved value is the fallback for a send that reports
      // neither way, and it deletes nothing.
      let result: void | boolean | Promise<void | boolean>;
      try {
        result = invoke(pending.acceptSend, pending.handOffToResend, pending.restoreSend, pending.markHostReturned);
      } catch {
        pending.finalizeSend(false);
        return;
      }
      settleSendResult(pending, () => result);
    },
    [beginSend],
  );

  // Sweep expired drafts once when mounted with a draftKey.
  const hasDraftKey = Boolean(draftKey);
  useEffect(() => {
    if (hasDraftKey) {
      pruneExpired();
    }
  }, [hasDraftKey]);

  // Flush current content synchronously before draftKey/userId changes or on unmount,
  // and restore (or migrate post-send continuation) when draftKey/userId changes.
  useEffect(() => {
    const prev = prevTargetRef.current;
    const draftKeyChanged = prev.draftKey !== draftKey;
    const userIdChanged = prev.userId !== userId;

    if (draftKeyChanged || userIdChanged) {
      prevTargetRef.current = { draftKey, userId };

      // DALTON-HOOK(composer-drafts): the offered unresolved-send copy follows
      // the key — what the new key's slot holds shows above the box. A send
      // still out owns its slot (migrated below) and shows no banner.
      setInFlightCopyState(
        prefillMessageRef.current || hadPrefillRef.current
          ? null
          : (draftKey ? loadInFlightCopy(draftKey, userId) : null),
      );

      // Migrate post-send continuation draft when `ws:<id>:new` resolves to `thread:<id>`
      // only if a send actually occurred on `ws:<id>:new`.
      if (
        draftKeyChanged &&
        hasSentOnKeyRef.current &&
        isNewWorkspaceDraftKey(prev.draftKey) &&
        isThreadDraftKey(draftKey)
      ) {
        const inFlight = inFlightSendRef.current;
        const sendStillInFlight =
          inFlight !== null &&
          !inFlight.settled &&
          inFlight.draftKey === prev.draftKey;
        if (sendStillInFlight && inFlight) {
          // The send's copy moves with it, under its own key; the live draft
          // on the old key was cleared at send (or holds what was typed
          // since, which the continuation below carries over).
          migrateInFlightCopy(prev.draftKey, draftKey, prev.userId, userId);
          inFlight.draftKey = draftKey;
          inFlight.userId = userId;
          // The send now owns its new key's generation (it migrated with it).
          latestSendByKeyRef.current.set(`${userId ?? ''}|${draftKey}`, inFlight);
          const followUpText = writtenSinceMountOrSendRef.current
            ? latestMessageRef.current
            : '';
          hasSentOnKeyRef.current = false;
          hadPrefillRef.current = Boolean(prefillMessageRef.current);
          applyRestored(null);
          onDraftKeySwitchRef.current?.();
          if (followUpText && followUpText.trim()) {
            save(draftKey, followUpText, userId, undefined, { parts: partsForSave() });
            latestMessageRef.current = followUpText;
            dirtyRef.current = false;
            writtenSinceMountOrSendRef.current = true;
            setMessageState(followUpText);
          } else {
            latestMessageRef.current = '';
            dirtyRef.current = false;
            writtenSinceMountOrSendRef.current = false;
            setMessageState('');
          }
          setInFlightCopyState(null);
          return () => {
            if (timerRef.current !== null) {
              clearTimeout(timerRef.current);
              timerRef.current = null;
            }
            if (dirtyRef.current && draftKey) {
              save(draftKey, latestMessageRef.current, userId, undefined, { parts: partsForSave() });
              dirtyRef.current = false;
            }
          };
        }

        const continuationText = writtenSinceMountOrSendRef.current
          ? latestMessageRef.current || load(prev.draftKey, prev.userId)
          : load(prev.draftKey, prev.userId);
        hasSentOnKeyRef.current = false;
        writtenSinceMountOrSendRef.current = false;
        hadPrefillRef.current = Boolean(prefillMessageRef.current);
        applyRestored(null);
        onDraftKeySwitchRef.current?.();

        if (continuationText && continuationText.trim()) {
          clearStorageEntry(prev.draftKey, prev.userId);
          save(draftKey, continuationText, userId, undefined, { parts: partsForSave() });
          latestMessageRef.current = continuationText;
          dirtyRef.current = false;
          writtenSinceMountOrSendRef.current = true;
          setMessageState(continuationText);
          return () => {
            if (timerRef.current !== null) {
              clearTimeout(timerRef.current);
              timerRef.current = null;
            }
            if (dirtyRef.current && draftKey) {
              save(draftKey, latestMessageRef.current, userId, undefined, { parts: partsForSave() });
              dirtyRef.current = false;
            }
          };
        }
      }

      if (draftKeyChanged) {
        hasSentOnKeyRef.current = false;
        writtenSinceMountOrSendRef.current = false;
        hadPrefillRef.current = Boolean(prefillMessageRef.current);
        applyRestored(null);
        onDraftKeySwitchRef.current?.();
      }
      const hasExternalPrefill =
        Boolean(prefillMessageRef.current) ||
        (!draftKeyChanged && hadPrefillRef.current);
      if (
        !hasExternalPrefill &&
        !(userIdChanged && !draftKeyChanged && dirtyRef.current)
      ) {
        const restoredRecord = draftKey ? loadStoredDraft(draftKey, userId) : null;
        const restored = restoredRecord?.text ?? '';
        latestMessageRef.current = restored;
        dirtyRef.current = false;
        applyRestored(restored ? restoredRecord : null);
        setMessageState(restored);
      }
    }

    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (dirtyRef.current && draftKey) {
        save(draftKey, latestMessageRef.current, userId, undefined, { parts: partsForSave() });
        dirtyRef.current = false;
      }
    };
  }, [draftKey, userId, applyRestored, partsForSave]);

  return {
    message,
    setMessage,
    restoredQuotes,
    restoredHadAttachments,
    inFlightCopy,
    restoreInFlightCopy,
    discardInFlightCopy,
    clearCurrentDraft,
    touchDraft,
    beginSend,
    submitWithDraft,
  };
}
