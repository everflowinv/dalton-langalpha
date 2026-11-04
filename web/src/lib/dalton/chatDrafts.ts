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
  inFlight: z.optional(z.boolean()),
  /** Quote mentions held in the draft, token included, so a restore brings the chips back. */
  quotes: z.optional(z.array(StoredDraftQuoteSchema)),
  /** The draft had attachments when it was saved; they are not kept, so a restore says to add them again. */
  hadAttachments: z.optional(z.boolean()),
});

export type StoredChatDraft = z.output<typeof StoredChatDraftSchema>;

/** The rich parts of a draft beyond its text, read at save time from the composer. */
export interface DraftParts {
  quotes?: QuoteMention[];
  hadAttachments?: boolean;
}

export interface SaveDraftOptions {
  inFlight?: boolean;
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
  const normalizedTargetUser =
    userId !== undefined ? normalizeUserId(userId) : null;
  for (const controller of activeControllers) {
    if (
      controller.getDraftKey() === draftKey &&
      (normalizedTargetUser === null ||
        controller.getUserId() === normalizedTargetUser)
    ) {
      clearStorageEntry(draftKey, controller.getUserId());
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
    ...(options?.inFlight ? { inFlight: true } : {}),
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

/**
 * Scan `localStorage` for `dalton.chatDraft.v1:*` and `dalton.chatDraft.v2:*`
 * keys and remove any expired (> 7 days) or malformed entries.
 */
export function pruneExpired(now: number = Date.now()): number {
  const storage = getLocalStorage();
  if (!storage) return 0;
  let removed = 0;
  try {
    const keys: string[] = [];
    const prefixes = [DRAFT_STORAGE_PREFIX, DRAFT_STORAGE_PREFIX_V1];
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
        const parsed = parseStoredDraft(raw);
        if (
          !parsed ||
          !parsed.text.trim() ||
          now - parsed.savedAt > DRAFT_TTL_MS
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
   * DALTON-HOOK(composer-drafts): the host takes a refused send back itself
   * (the chat's returnedDraft, re-seated through its giveBack) while the
   * composer sits where the transcript is out of view. Read when a send
   * settles failed: then this settle keeps only the stored draft — the input
   * is the host's to restore. Without it (the plain chat view) the composer
   * restores what the send took, through `onSendReturned`.
   */
  hostRestoresUnsent?: () => boolean;
  /**
   * DALTON-HOOK(composer-drafts): re-seats a failed send's text and quote
   * chips into the composer, collision-safe against quotes added since (one
   * may have taken the same token). Called by the failure settle only when no
   * host giveBack owns the restore.
   */
  onSendReturned?: (sentText: string, parts: DraftParts) => void;
}

export interface PendingSendHandle {
  finalizeSend: (succeeded: boolean) => void;
  /**
   * Settle this send as accepted before its promise resolves: the server has
   * taken it (a run_id arrived, a steer was accepted, the send was queued, a
   * duplicate was adopted, …), so its draft is safe to clear even though the
   * stream may run on for a long time — a reload mid-stream must not restore
   * an already-sent message into the composer. A later finalizeSend on the
   * same handle is a no-op.
   */
  acceptSend: () => void;
  /**
   * DALTON-HOOK(composer-drafts): a drop scheduled this send's own automatic
   * resend — the resend is what can prove the server took the message, not
   * the response headers it dropped after. From here only the resend's
   * acceptance (`acceptSend`, through the acceptance callback the resent
   * attempts carry) settles this handle; the promise's own value settles
   * nothing, and the stored draft keeps its inFlight mark so a reload
   * restores the text with the notice.
   */
  handOffToResend: () => void;
}

interface InFlightSendState {
  draftKey?: string;
  userId: string;
  sentText: string;
  settled: boolean;
  /** True once handOffToResend parked it: only acceptance settles it from there. */
  handedOff?: boolean;
  /** The draft's rich parts at send: what its restore brings back. */
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
  restoredFromInFlight: boolean;
  /** Quotes the restored draft carried, a fresh array per restore; seed them into the composer's mentions once. */
  restoredQuotes: QuoteMention[];
  /** The restored draft had attachments, which are not kept; the composer says to add them again. */
  restoredHadAttachments: boolean;
  clearCurrentDraft: () => void;
  /**
   * DALTON-HOOK(composer-drafts): something beside the text changed (an
   * attachment was added or removed) that the stored draft carries — saves the
   * draft on the same debounce, unless a send still owns the box.
   */
  touchDraft: () => void;
  beginSend: () => PendingSendHandle;
  submitWithDraft: (
    invoke: (onAccepted: () => void, onHandedOff: () => void) => void | boolean | Promise<void | boolean>,
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
  hostRestoresUnsent,
  onSendReturned,
}: UseComposerDraftOptions): UseComposerDraftResult {
  const { user } = useUser({ enabled: Boolean(draftKey) });
  const userId = normalizeUserId(user?.user_id);

  const [message, setMessageState] = useState<string>(() => {
    if (prefillMessage) return prefillMessage;
    if (!draftKey) return '';
    return load(draftKey, userId);
  });
  const [restoredFromInFlight, setRestoredFromInFlightState] = useState<boolean>(() => {
    if (prefillMessage || !draftKey) return false;
    return loadStoredDraft(draftKey, userId)?.inFlight === true;
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
  const restoredFromInFlightRef = useRef<boolean>(restoredFromInFlight);
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
  const hostRestoresUnsentRef = useRef<(() => boolean) | undefined>(hostRestoresUnsent);
  const onSendReturnedRef = useRef<((sentText: string, parts: DraftParts) => void) | undefined>(onSendReturned);
  /** Mirrors `restoredHadAttachments`, for a save to keep the attachments-again notice standing. */
  const restoredHadAttachmentsRef = useRef<boolean>(false);
  const prevTargetRef = useRef<{ draftKey?: string; userId: string }>({
    draftKey,
    userId,
  });

  const updateRestoredFromInFlight = useCallback((next: boolean) => {
    restoredFromInFlightRef.current = next;
    setRestoredFromInFlightState(next);
  }, []);

  /** Applies what a restore brought back; `null` is every reset point (sent, cleared, switched). */
  const applyRestored = useCallback(
    (record: StoredChatDraft | null) => {
      updateRestoredFromInFlight(Boolean(record && record.inFlight));
      setRestoredQuotesState(record?.quotes ?? []);
      restoredHadAttachmentsRef.current = record?.hadAttachments === true;
      setRestoredHadAttachmentsState(restoredHadAttachmentsRef.current);
    },
    [updateRestoredFromInFlight],
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
    hostRestoresUnsentRef.current = hostRestoresUnsent;
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
        // Parts read at flush: what came back through giveBack after a failed
        // send (mentions, files) has landed by the time the debounce ends.
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
      const wasInFlight = restoredFromInFlightRef.current;
      latestMessageRef.current = next;
      updateRestoredFromInFlight(false);
      setMessageState(next);
      if (wasInFlight && activeDraftKeyRef.current && next && next.trim()) {
        save(activeDraftKeyRef.current, next, activeUserIdRef.current, undefined, { parts: partsForSave() });
      }
      scheduleSave(next);
    },
    [scheduleSave, updateRestoredFromInFlight, partsForSave],
  );

  /**
   * DALTON-HOOK(composer-drafts): something beside the text changed (an
   * attachment was added or removed) that the stored draft carries — saves on
   * the same debounce, unless a send still owns the box: its own settle writes
   * the draft, and a clear here would delete the inFlight copy a reload
   * restores by.
   */
  const touchDraft = useCallback(() => {
    const inFlight = inFlightSendRef.current;
    if (inFlight && !inFlight.settled) return;
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

    // Ensure the draft is persisted in localStorage while awaiting send completion,
    // and clear the input box programmatically without deleting the stored draft.
    if (inFlight.draftKey && sentText.trim()) {
      save(inFlight.draftKey, sentText, inFlight.userId, Date.now(), { inFlight: true, parts });
    }
    dirtyRef.current = false;
    writtenSinceMountOrSendRef.current = false;
    hasSentOnKeyRef.current = true;
    latestMessageRef.current = '';
    applyRestored(null);
    setMessageState('');

    const settleSuccess = () => {
      const sendDraftKey = inFlight.draftKey;
      const sendUserId = inFlight.userId;
      const isSameKey = activeDraftKeyRef.current === sendDraftKey;
      const hasFollowUp =
        isSameKey &&
        (writtenSinceMountOrSendRef.current ||
          Boolean(latestMessageRef.current.trim()));
      if (!hasFollowUp && sendDraftKey) {
        clearStorageEntry(sendDraftKey, sendUserId);
      }
    };

    const settle = (succeeded: boolean, fromAcceptance = false) => {
      if (inFlight.settled) return;
      // A parked send answers only to its resend's acceptance; the promise's
      // value (and any failure settle) settles nothing while the resend may
      // still be the copy the server keeps.
      if (inFlight.handedOff && !fromAcceptance) return;
      inFlight.settled = true;
      // A newer send on this key may have superseded this one while it was in
      // flight (a steer sent mid-stream, say): the key's stored draft and the
      // input are that send's now — this settle touches neither, or the first
      // send's completion would delete or restore the second send's draft.
      // Supersession is per key: a newer send on another key (the market panel's key moves
      // from its workspace form to the new thread's once the thread exists) does not own
      // this send's stored draft, so this send still settles its own key.
      const latestOnKey = inFlight.draftKey
        ? latestSendByKeyRef.current.get(`${inFlight.userId ?? ''}|${inFlight.draftKey}`)
        : undefined;
      if (latestOnKey && latestOnKey !== inFlight) return;
      if (inFlightSendRef.current === inFlight) inFlightSendRef.current = null;

      if (succeeded) {
        settleSuccess();
        return;
      }
      const sendDraftKey = inFlight.draftKey;
      const sendUserId = inFlight.userId;
      if (!sendDraftKey) {
        return;
      }
      // Send failed: keep the draft in localStorage and put the original text back.
      if (activeDraftKeyRef.current === sendDraftKey) {
        hasSentOnKeyRef.current = false;
        if (timerRef.current !== null) {
          clearTimeout(timerRef.current);
          timerRef.current = null;
        }
        const trimmedSent = sentText.trim();
        const currentTyped = latestMessageRef.current.trim();
        const alreadyRestored =
          Boolean(trimmedSent) &&
          (currentTyped === trimmedSent ||
            currentTyped.startsWith(`${trimmedSent}\n\n`) ||
            currentTyped.endsWith(`\n\n${trimmedSent}`));
        const restoredText = sentText
          ? alreadyRestored
            ? latestMessageRef.current
            : currentTyped
              ? `${sentText}\n\n${latestMessageRef.current}`
              : sentText
          : latestMessageRef.current;
        if (restoredText.trim()) {
          save(sendDraftKey, restoredText, sendUserId, undefined, { parts: inFlight.parts });
        }
        if (restoredText) {
          // DALTON-HOOK(composer-drafts): who re-seats the input depends on
          // where the composer sits. A host that takes refused sends back
          // itself (the chat's returnedDraft → giveBack, while the transcript
          // is out of view) owns the input — this settle only keeps the stored
          // draft, so a reload before its restore lands still finds the text.
          // Otherwise the composer restores its own send: the quotes go back
          // collision-safe through onSendReturned (a quote added since may
          // have taken the same token), and the attachments-again notice
          // stands on the box until files are beside the draft again.
          const restore = onSendReturnedRef.current;
          if (hostRestoresUnsentRef.current?.()) {
            latestMessageRef.current = restoredText;
            dirtyRef.current = false;
            writtenSinceMountOrSendRef.current = true;
            updateRestoredFromInFlight(false);
          } else if (restore) {
            dirtyRef.current = false;
            writtenSinceMountOrSendRef.current = true;
            applyRestored({ text: restoredText, savedAt: 0, hadAttachments: inFlight.parts.hadAttachments });
            restore(sentText, inFlight.parts);
          } else {
            latestMessageRef.current = restoredText;
            dirtyRef.current = false;
            writtenSinceMountOrSendRef.current = true;
            updateRestoredFromInFlight(false);
            setMessageState(restoredText);
          }
        }
      } else if (sentText.trim()) {
        save(sendDraftKey, sentText, sendUserId, undefined, { parts: inFlight.parts });
      }
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
    };
  }, [updateRestoredFromInFlight, partsForSave]);

  const submitWithDraft = useCallback(
    (invoke: (onAccepted: () => void, onHandedOff: () => void) => void | boolean | Promise<void | boolean>) => {
      const pending = beginSend();
      // The send path reports acceptance early (a run started, a steer was
      // taken, the send was queued) by calling onAccepted, and a drop that
      // scheduled the send's own resend by calling onHandedOff — from then the
      // resend's acceptance is the only thing that settles it. The promise's
      // resolved value is the fallback for a send that reports neither.
      let result: void | boolean | Promise<void | boolean>;
      try {
        result = invoke(pending.acceptSend, pending.handOffToResend);
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
          clearStorageEntry(prev.draftKey, prev.userId);
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
            if (inFlight.sentText.trim()) {
              save(draftKey, inFlight.sentText, userId, Date.now(), { inFlight: true, parts: inFlight.parts });
            }
            latestMessageRef.current = '';
            dirtyRef.current = false;
            writtenSinceMountOrSendRef.current = false;
            setMessageState('');
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
    restoredFromInFlight,
    restoredQuotes,
    restoredHadAttachments,
    clearCurrentDraft,
    touchDraft,
    beginSend,
    submitWithDraft,
  };
}
